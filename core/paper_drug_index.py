"""Find a paper's drug in the database: one index, one ordered set of lookups.

Paper Query extracts each drug a paper names (its name and some alternate names, from the
extraction model) and must find the database report for it. A wrong match is worse than no
match: no person reviews the match before the verdict is shown. So:

- one drug found  -> match;
- several found   -> no match, but the candidates are returned ("possible matches");
- nothing found   -> no match.

A last lookup, tried only when everything else found nothing, uses the short synonyms (under
MIN_ALIAS_LEN). A short synonym is accepted only when the paper's target gene appears in our
report for that drug: short codes ("C10", "ZN-c3") are often a paper's own label.

One exception to "several found -> no match": when the drugs found are one compound held more
than once (a salt and its free form, same parent InChIKey), the salt entry is taken, but only
when its structure is that compound plus an ordinary counter-ion and only its name says salt.

There is no model call, no regex guessing on the paper's side, and no hand-written list:
the same input always gives the same result. tests/paper_query/test_paper_drug_index.py has
a test for each rule; run it after any change to the order of the lookups.
"""
import collections
import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy.exc import SQLAlchemyError

from .db import (
    AnalysisResult,
    DrugSynonym,
    SynonymIndex,
    _SALT_FORMS,
    _SALT_SUFFIXES,
    _is_generic_name,
    init_db,
    normalize_key,
    stereo_key,
    strip_salt_suffix,
)

logger = logging.getLogger(__name__)

# Kinds of index entry. A drug's own name is the strongest evidence; the others come second.
NAME = "name"            # the database drug's own name
NAME_BASE = "name-base"  # our name without its salt word, or the part before / inside a bracket
ALIAS = "alias"          # a synonym from synonym_index
SHORT_ALIAS = "short-alias"  # a synonym under MIN_ALIAS_LEN, confirmed by the paper's target gene
SALT_FORM = "salt-form"      # several entries were one compound; the salt entry was taken

# What one lookup searches: the drug names only, or every other kind (base names and synonyms).
NAMES_ONLY = "names-only"
OTHER_NAMES = "other-names"

# Aliases shorter than this (without stereo descriptors) are kept out of the ordinary lookups:
# short acronyms collide across compounds ('d3', 'cpt'), and papers use short labels ("C10",
# "P4") for their own compounds. They are used only by the last, target-confirmed lookup.
# Bare numbers (CIDs, CAS fragments) are never indexed.
MIN_ALIAS_LEN = 5

# The paper's target gene, as gene-symbol-like words. Plain words that the extraction sometimes
# writes in the gene field are not genes.
_GENE_RE = re.compile(r"[A-Z][A-Z0-9]{1,9}")
_NOT_GENES = frozenset({"NONE", "NA", "AND", "OR", "NOT", "UNKNOWN", "ALL", "OTHER", "THE", "OF"})

# Salt-form fallback. Counter-ions and solvents a salt entry may carry besides its parent
# compound (inorganic ions always may). A salt whose structure has anything else is a
# combination product (Carbasalate Calcium = aspirin + urea; Acrisorcin; Inosine Pranobex) and
# is never taken for its parent.
_COUNTER_ION_SMILES = (
    "CC(=O)O", "OC(=O)C(O)C(O)C(=O)O", "OC(=O)C=CC(=O)O", "OC(=O)CC(O)(CC(=O)O)C(=O)O",
    "OC(=O)C(=O)O", "CS(=O)(=O)O", "Cc1ccc(S(=O)(=O)O)cc1", "OS(=O)(=O)c1ccccc1",
    "OC(=O)C(F)(F)F", "COS(=O)(=O)O", "OC(=O)C(O)C(O)C(O)C(O)C(=O)O",
    "OS(=O)(=O)c1cccc2c(S(=O)(=O)O)cccc12", "NCCCCC(N)C(=O)O", "NC(N)=NCCCC(N)C(=O)O",
    "NC(CO)(CO)CO", "C[N+](C)(C)CCO", "CC(C)(C)N", "CNCC(O)C(O)C(O)C(O)CO", "OC(=O)CCC(=O)O",
    "CC(O)C(=O)O", "OCC(O)C(O)C(O)C(O)C(=O)O", "OC(=O)C(c1ccccc1)(c1ccccc1)c1ccccc1", "CS(C)=O",
    "CC(C)=O", "CCO", "OC(=O)c1cc(=O)[nH]c(=O)[nH]1", "c1ccc(CNCCNCc2ccccc2)cc1", "OCCN",
    "OS(=O)(=O)O", "OC(=O)CC(O)C(=O)O")
# Words that mark a name as a salt, solvate or hydrate entry.
_SALT_NAME_WORDS = frozenset(
    w for w in (x.lower() for x in _SALT_SUFFIXES) if " " not in w) | frozenset({
    "salt", "bitartrate", "monosodium", "ammonium", "lithium", "metilsulfate", "methylsulfate",
    "mucate", "napadisylate", "tromethamine", "lysine", "lysinate", "arginine", "erbumine",
    "sesquifumarate", "dimaleate", "orotate", "benzathine", "trifenatate", "solvate", "acetone",
    "p-toluenesulfonate", "choline", "hcl", "hbr"})

# The one generic-looking name that is let through: "Compound C" is the
# usual literature name for dorsomorphin. The ordinary synonym lookup then finds it.
GENERIC_EXEMPT = frozenset({normalize_key("Compound C")})

# normalize_key drops every non-alphanumeric character, so "(+)-X", "(-)-X" and "X" collide.
# The index keeps the sign descriptors as words at the end of the key instead.
_NUMERIC_RANGE_RE = re.compile(r"^[\d\-:]+$")


def _bare(key: str) -> str:
    return key.split("|", 1)[0]


def _base_names(name: str) -> List[str]:
    """Shorter forms of one of OUR drug names: without its salt word, and the part before and the
    parts inside a trailing bracket ('Sodium dichloroacetate (DCA)' -> 'Sodium dichloroacetate',
    'DCA'). Our names are known, so this is deterministic. Generic labels, salt words and number
    ranges are skipped."""
    out = []
    working = strip_salt_suffix(name)
    if working != name:
        out.append(working)
    idx = working.rfind(" (")
    if idx > 0 and working.endswith(")"):
        base, inner = working[:idx].strip(), working[idx + 2:-1]
        if base and not _is_generic_name(base):
            out.append(base)
        for part in inner.split(","):
            part = part.strip()
            if (len(part) >= 2 and not _is_generic_name(part)
                    and normalize_key(part) not in _SALT_FORMS
                    and not _NUMERIC_RANGE_RE.match(part)):
                out.append(part)
    return out


class DrugIndex:
    """Every database drug, reachable by its own name and by its other names.

    `names`:  stereo_key -> {drug_name}                (kind NAME; searched by NAMES_ONLY)
    `others`: stereo_key -> {drug_name: {kind, ...}}   (kinds NAME_BASE, ALIAS; OTHER_NAMES)
    `short`:  stereo_key -> {drug_name}                (short synonyms; the last lookup only)

    Several drugs under one key are kept: the lookup reports them as possible matches.
    `report_text(drug)` gives the text the last lookup searches for the paper's gene (our
    report's target, gene list and report); without it, short synonyms are never used.
    `identity` maps a drug to (inchikey, parent_inchikey, smiles), for the salt-form fallback;
    without it, several drugs always stay possible matches."""

    def __init__(self, drug_names: Iterable[str], aliases: Iterable[Tuple[str, str]] = (),
                 report_text: Optional[Callable[[str], str]] = None,
                 identity: Optional[Dict[str, Tuple[str, str, str]]] = None):
        self.names: Dict[str, Set[str]] = collections.defaultdict(set)
        self.others: Dict[str, Dict[str, Set[str]]] = collections.defaultdict(
            lambda: collections.defaultdict(set))
        self.short: Dict[str, Set[str]] = collections.defaultdict(set)
        self._report_text = report_text
        self._text_cache: Dict[str, str] = {}
        self._identity = identity or {}
        self._clean_salt_cache: Dict[str, bool] = {}
        known = set()
        for name in drug_names:
            if not name:
                continue
            known.add(name)
            self.names[stereo_key(name)].add(name)
            for base in _base_names(name):
                self.others[stereo_key(base)][name].add(NAME_BASE)
        for alias, drug in aliases:
            if not alias or drug not in known:
                continue
            key = stereo_key(alias)
            bare = _bare(key)
            if bare.isdigit() or len(bare) < 2:
                continue
            if len(bare) < MIN_ALIAS_LEN:
                self.short[key].add(drug)
                continue
            self.others[key][drug].add(ALIAS)

    @classmethod
    def from_db(cls) -> "DrugIndex":
        """Build from analysis_results and synonym_index. Build it once per paper (or once per
        batch run) and pass it in; it reads the whole synonym table (about 2 s)."""
        Session = init_db()
        s = Session()
        try:
            names = [n for (n,) in s.query(AnalysisResult.drug_name).order_by(AnalysisResult.id)]
            try:
                aliases = s.query(SynonymIndex.alias_raw, SynonymIndex.canonical_drug_name).all()
            except SQLAlchemyError as e:      # a database without the identity tables
                logger.warning("synonym_index unavailable (%s): matching on names only",
                               e.__class__.__name__)
                s.rollback()
                aliases = []
            try:
                identity = {n: (ik or "", pik or "", smi or "") for n, ik, pik, smi in s.query(
                    DrugSynonym.drug_name, DrugSynonym.inchikey, DrugSynonym.parent_inchikey,
                    DrugSynonym.smiles) if ik or pik}
            except SQLAlchemyError as e:
                logger.warning("drug_synonyms unavailable (%s): no salt-form fallback",
                               e.__class__.__name__)
                s.rollback()
                identity = {}
        finally:
            s.close()
        return cls(names, aliases, report_text=_report_text_from_db(Session), identity=identity)

    def lookup(self, name: str, scope: str, stripped: bool) -> Dict[str, str]:
        """One lookup. scope NAMES_ONLY searches the drug names; OTHER_NAMES searches the base
        names and synonyms. stripped -> the name without its salt word (empty if it has none).
        Returns {drug_name: kind}, kind being NAME, NAME_BASE or ALIAS."""
        form = strip_salt_suffix(name) if stripped else name
        if stripped and form == name:
            return {}
        key = stereo_key(form)
        if scope == NAMES_ONLY:
            return {d: NAME for d in self.names.get(key, ())}
        found = {}
        for d, kinds in self.others.get(key, {}).items():
            found[d] = NAME_BASE if NAME_BASE in kinds else ALIAS
        return found

    def salt_form(self, drugs: Iterable[str]) -> Optional[str]:
        """When `drugs` are one compound (same parent InChIKey) and exactly one of them is a plain
        salt of it whose name says so, while no other name does: that one. Otherwise None."""
        drugs = sorted(drugs)
        parents = {_parent_key(self._identity.get(d)) for d in drugs}
        if len(parents) != 1 or "" in parents:
            return None
        named = [d for d in drugs if _has_salt_word(d)]
        if len(named) != 1 or not self._is_clean_salt(named[0]):
            return None
        return named[0]

    def _is_clean_salt(self, drug: str) -> bool:
        if drug not in self._clean_salt_cache:
            self._clean_salt_cache[drug] = _is_clean_salt(*self._identity.get(drug, ("", "", "")))
        return self._clean_salt_cache[drug]

    def report_mentions(self, drug: str, genes: Set[str]) -> bool:
        """Does our report for `drug` name one of `genes` as a whole word (case as written)?"""
        if self._report_text is None:
            return False
        if drug not in self._text_cache:
            self._text_cache[drug] = self._report_text(drug) or ""
        text = self._text_cache[drug]
        return any(re.search(rf"(?<![A-Za-z0-9]){re.escape(g)}(?![A-Za-z0-9])", text) for g in genes)


def _report_text_from_db(Session) -> Callable[[str], str]:
    """One drug's target, gene list and report, read when the last lookup needs it (rarely), so
    the index never loads every report."""
    def text(drug: str) -> str:
        s = Session()
        try:
            row = (s.query(AnalysisResult.target, AnalysisResult.target_gene_names_list,
                           AnalysisResult.pi_report)
                   .filter(AnalysisResult.drug_name == drug).first())
        except SQLAlchemyError as e:
            logger.warning("report text for %r unavailable (%s)", drug, e.__class__.__name__)
            s.rollback()
            row = None
        finally:
            s.close()
        return " ".join(str(x) for x in row if x) if row else ""
    return text


def _parent_key(ident) -> str:
    if not ident:
        return ""
    inchikey, parent, _ = ident
    return parent or inchikey


def _has_salt_word(name: str) -> bool:
    """The name marks a salt, solvate or hydrate entry. "free base" does not count."""
    low = name.lower().strip()
    if low.endswith("free base"):
        return False
    if strip_salt_suffix(name) != name:
        return True
    words = re.sub(r"[-(),]", " ", low).split()
    return any(w in _SALT_NAME_WORDS for w in words)


_COUNTER_ION_KEYS: Optional[Set[str]] = None


def _is_clean_salt(inchikey: str, parent: str, smiles: str) -> bool:
    """A salt of `parent` by structure (its own InChIKey differs) whose every carbon-containing
    part is the parent itself or a listed counter-ion or solvent."""
    global _COUNTER_ION_KEYS
    if not (inchikey and parent and smiles) or inchikey == parent:
        return False
    from rdkit import Chem, RDLogger                      # noqa: PLC0415 (only needed here)
    from rdkit.Chem.MolStandardize import rdMolStandardize  # noqa: PLC0415
    RDLogger.DisableLog("rdApp.*")
    if _COUNTER_ION_KEYS is None:
        _COUNTER_ION_KEYS = {Chem.MolToInchiKey(Chem.MolFromSmiles(x))[:14]
                             for x in _COUNTER_ION_SMILES}
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return False
    uncharger = rdMolStandardize.Uncharger()
    for part in Chem.GetMolFrags(mol, asMols=True):
        if not any(a.GetSymbol() == "C" for a in part.GetAtoms()):
            continue                                       # an inorganic ion
        key = Chem.MolToInchiKey(uncharger.uncharge(part))[:14]
        if key != parent[:14] and key not in _COUNTER_ION_KEYS:
            return False
    return True


def _paper_genes(field) -> Set[str]:
    """Gene-symbol-like words in the paper's human_target_gene field."""
    if not isinstance(field, str):
        return set()
    return {g for g in _GENE_RE.findall(field.upper()) if g not in _NOT_GENES}


@dataclass
class Match:
    """The outcome for one paper drug. `drug` is set only when exactly one drug was found."""
    drug: Optional[str] = None
    via: Optional[str] = None                 # NAME, NAME_BASE, ALIAS, SHORT_ALIAS or SALT_FORM
    matched_name: Optional[str] = None        # the paper name or alternate that found it
    salt_stripped: bool = False
    possible: List[str] = field(default_factory=list)   # set when several drugs were found


# _is_generic_name calls every name whose key is all digits "a bare number". A code with
# separators between its digit groups ("666-15") is a drug name: we hold a drug called that.
_DIGIT_CODE_RE = re.compile(r"\d+(?:\s*[-–]\s*\d+)+")


def _usable(name) -> bool:
    if not isinstance(name, str):
        return False
    key = normalize_key(name)
    if not key or key == "none":
        return False
    if key in GENERIC_EXEMPT or _DIGIT_CODE_RE.fullmatch(name.strip()):
        return True
    return not _is_generic_name(name)


def resolve(drug_data: Dict, index: DrugIndex) -> Match:
    """Find the database drug for one extracted paper drug.

    Order (first lookup that finds anything decides):
      1. the primary name: as written vs drug names, as written vs other names, then without its
         salt word vs drug names, then vs other names;
      2. the alternate names: the same four lookups, each across ALL alternates before the next
         (so an exact drug name on any alternate beats an alias on an earlier one);
      3. only if 1 and 2 found nothing: the short synonyms, primary name then alternates. The
         first name with any short-synonym drug decides; a drug counts only if our report names
         the paper's target gene. Without a paper gene this step is skipped.
    When a lookup in 1 or 2 finds several drugs that are one compound, DrugIndex.salt_form may
    pick the salt entry (via SALT_FORM); otherwise they are returned as possible matches.
    A generic primary name ("Compound 9") stops the match. Exceptions: "Compound C", and codes
    with hyphens between digit groups ("666-15")."""
    primary = drug_data.get("name") or ""
    if not _usable(primary):
        return Match()
    alternates = [a for a in (drug_data.get("alternate_names") or []) if _usable(a)]
    steps = [(stripped, scope) for stripped in (False, True) for scope in (NAMES_ONLY, OTHER_NAMES)]
    for names in ([primary], alternates):
        for stripped, scope in steps:
            for n in names:
                found = index.lookup(n, scope, stripped)
                if len(found) == 1:
                    (drug, how), = found.items()
                    return Match(drug=drug, via=how, matched_name=n, salt_stripped=stripped)
                if len(found) > 1:
                    salt = index.salt_form(found)
                    if salt:
                        return Match(drug=salt, via=SALT_FORM, matched_name=n, salt_stripped=stripped)
                    return Match(possible=sorted(found))
    genes = _paper_genes(drug_data.get("human_target_gene"))
    if not genes or not index.short:
        return Match()
    for n in [primary] + alternates:
        found = index.short.get(stereo_key(n))
        if not found:
            continue
        confirmed = sorted(d for d in found if index.report_mentions(d, genes))
        if len(confirmed) == 1:
            return Match(drug=confirmed[0], via=SHORT_ALIAS, matched_name=n)
        if len(confirmed) > 1:
            return Match(possible=confirmed)
        return Match()
    return Match()
