"""
Filename cleaning and NZDWorld branding (brief section 5).

The rules are deliberately conservative: a word is only removed when it is a
*whole separator-delimited token* that matches a configured brand rule or is
domain-shaped. A movie called "Extraction" is never damaged because some site
happens to be called "Extrac".

Public entry point::

    clean_filename(
        "Extraction-3-2026-nkiri.com.mkv",
        title="Extraction 3",
        mode="auto",
        brand_rules=[("token", "nkiri")],
    )
    -> FilenameResult(filename="extraction-3-2026 | NZDWorld.com.mkv", ...)

The function is pure (no database, no settings import) so it can be unit tested
in isolation and reused by the API preview endpoint.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

DEFAULT_BRAND_SUFFIX = "NZDWorld.com"

# A fixed, conservative TLD list. Only tokens that *end* in one of these and
# contain a dot are treated as a source-site domain; this keeps arbitrary dots
# inside titles intact.
DOMAIN_TLDS = (
    "com|net|org|info|biz|name|pro|xyz|top|site|online|link|live|club|vip|app|dev|"
    "shop|store|website|space|fun|icu|monster|cfd|click|download|stream|watch|movie|"
    "tv|cc|me|io|co|ng|uk|us|in|ru|to|ws|la|gg|is|fm|am|pm|tv|mobi|asia|pw|su|sx"
)

_DOMAIN_TLD_SET = frozenset(DOMAIN_TLDS.split("|"))

# Subdomain labels that are infrastructure rather than a brand, dropped along
# with the domain they belong to (www.example.com -> example.com -> gone).
_SUBDOMAIN_PREFIXES = frozenset(
    {"www", "cdn", "dl", "dls", "media", "files", "file", "download", "downloads", "uploads", "static"}
)

_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_EPISODE_RE = re.compile(r"^s\d{1,2}(?:e\d{1,3})?$|^e\d{1,3}$|^\d{1,2}x\d{1,3}$")

QUALITY_TOKENS = frozenset(
    {
        "480p", "540p", "576p", "720p", "1080p", "1440p", "2160p", "4320p", "2k", "4k", "8k",
        "hd", "fhd", "uhd", "sd", "hq", "web", "webrip", "webdl", "webhd", "bluray", "brrip",
        "bdrip", "dvdrip", "dvdscr", "hdtv", "hdrip", "hdcam", "cam", "camrip", "telesync",
        "ts", "tc", "remux", "x264", "x265", "h264", "h265", "hevc", "avc", "xvid", "divx",
        "aac", "ac3", "eac3", "dts", "dtshd", "truehd", "atmos", "flac", "mp3", "ddp", "dd",
        "10bit", "8bit", "hdr", "dv", "sdr", "proper", "repack", "internal", "dual", "audio",
    }
)

EDITION_TOKENS = frozenset(
    {
        "extended", "unrated", "uncut", "directors", "director", "theatrical", "remastered",
        "imax", "special", "edition", "collectors", "anniversary", "restored", "criterion",
        "final", "cut", "ultimate", "complete",
    }
)

_LANGUAGE_TOKENS = frozenset(
    {"eng", "english", "sub", "subs", "subbed", "dubbed", "dub", "multi", "yoruba", "hindi", "korean", "nolly"}
)

_SEPARATORS = " \t\r\n._-–—+~()[]{}|,/\\:;!?&*\"'`"

# Characters that must never appear in a stored filename.
_UNSAFE_RE = re.compile(r"[^0-9a-z]+")
_TRIM_DASHES_RE = re.compile(r"^-+|-+$")
_COLLAPSE_DASHES_RE = re.compile(r"-{2,}")
_COLLAPSE_SPACES_RE = re.compile(r"[ ]{2,}")


class FilenameError(ValueError):
    """Raised when a filename cannot be produced safely."""


@dataclass(frozen=True)
class FilenameResult:
    """Outcome of a cleaning operation, suitable for showing in the admin UI."""

    filename: str
    stem: str
    extension: str
    mode: str
    removed: tuple[str, ...] = field(default_factory=tuple)
    preserved: tuple[str, ...] = field(default_factory=tuple)


def split_extension(filename: str) -> tuple[str, str]:
    """Return ``(stem, extension)`` with the extension lower-cased and dot-free."""
    name = (filename or "").strip()
    if not name:
        raise FilenameError("Empty filename.")
    # Ignore a trailing dot ("movie." has no extension).
    if "." not in name.rstrip("."):
        return name.rstrip("."), ""
    stem, _, ext = name.rpartition(".")
    return stem, ext.strip().lower()


def _ascii(text: str) -> str:
    """Fold accents to ASCII so filenames stay portable."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return decomposed.encode("ascii", "ignore").decode("ascii")


def _strip_domains(text: str, removed: list[str]) -> str:
    """
    Remove trailing ``label.tld`` domain chains without eating title words.

    Works on dot-separated segments rather than a greedy regex, because a
    greedy match would swallow ``Extraction-3-2026-nkiri.com`` whole. Here the
    last hyphen part of the label is treated as the brand (``nkiri``) and the
    rest of the label (``Extraction-3-2026``) is kept.
    """
    parts = text.split(".")
    if len(parts) < 2:
        return text

    while len(parts) >= 2 and parts[-1].strip().lower() in _DOMAIN_TLD_SET:
        label = parts[-2]
        tld = parts[-1]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-]*", label):
            # Not a domain label (it contains spaces/separators), so only the
            # bare TLD is branding; leave the rest for the token filter.
            removed.append(tld)
            parts = parts[:-1]
            continue
        pieces = label.split("-")
        prefix, brand = "-".join(pieces[:-1]), pieces[-1]
        if not brand:
            break
        removed.append(f"{brand}.{tld}")
        parts = parts[:-2]
        if prefix:
            parts.append(prefix)
        # Drop an infrastructure subdomain sitting in front of the domain.
        if parts and parts[-1].strip().lower() in _SUBDOMAIN_PREFIXES:
            removed.append(parts[-1])
            parts = parts[:-1]

    return ".".join(parts)


def _tokenise(text: str) -> list[str]:
    """Split on anything that is not alphanumeric; keeps S01E03 as one token."""
    return [token for token in _UNSAFE_RE.split(_ascii(text).lower()) if token]


def _brand_tokens(brand_suffix: str) -> set[str]:
    """Tokens that make up the branding suffix, so it can never be duplicated."""
    tokens = set(_tokenise(brand_suffix))
    tokens.add("nzdworld")
    return tokens


def _rule_tokens(brand_rules) -> tuple[set[str], list[re.Pattern[str]]]:
    """
    Split configured rules into (literal tokens, compiled regexes).

    ``brand_rules`` accepts ``("token", "nkiri")`` / ``("regex", r"^bot[0-9]+$")``
    pairs or objects exposing ``kind`` and ``pattern`` attributes.
    """
    literals: set[str] = set()
    regexes: list[re.Pattern[str]] = []
    for rule in brand_rules or ():
        if hasattr(rule, "kind") and hasattr(rule, "pattern"):
            kind, pattern = rule.kind, rule.pattern
            active = getattr(rule, "is_active", True)
            if not active:
                continue
        else:
            try:
                kind, pattern = rule
            except (TypeError, ValueError):
                continue
        if kind == "regex":
            try:
                regexes.append(re.compile(pattern))
            except re.error:
                continue
        elif pattern:
            literals.update(_tokenise(pattern))
    return literals, regexes


def _is_preserved_metadata(token: str) -> bool:
    return bool(
        _YEAR_RE.match(token)
        or _EPISODE_RE.match(token)
        or token in QUALITY_TOKENS
        or token in EDITION_TOKENS
    )


def _filter_tokens(tokens, *, drop: set[str], regexes: list[re.Pattern[str]]):
    kept: list[str] = []
    removed: list[str] = []
    for token in tokens:
        if token in drop or any(rx.search(token) for rx in regexes):
            removed.append(token)
            continue
        kept.append(token)
    return kept, removed


def _join(tokens: list[str]) -> str:
    stem = "-".join(tokens)
    stem = _COLLAPSE_DASHES_RE.sub("-", stem)
    return _TRIM_DASHES_RE.sub("", stem)


def _truncate_stem(stem: str, max_len: int) -> str:
    if len(stem) <= max_len:
        return stem
    cut = stem[:max_len]
    if "-" in cut:
        cut = cut.rsplit("-", 1)[0]
    return _TRIM_DASHES_RE.sub("", cut)


def _apply_suffix(stem: str, brand_suffix: str) -> str:
    suffix = brand_suffix.strip()
    if not suffix:
        return stem
    return f"{stem} | {suffix}" if stem else suffix


def clean_filename(
    original_name: str,
    *,
    title: str | None = None,
    mode: str = "auto",
    brand_rules=(),
    brand_suffix: str = DEFAULT_BRAND_SUFFIX,
    allowed_extensions=None,
    max_stem_length: int = 140,
) -> FilenameResult:
    """
    Produce a branded, filesystem-safe filename.

    Parameters
    ----------
    original_name:
        The filename as delivered by the source (extension is trusted for the
        extension only, never for the content).
    title:
        Movie title. Required for ``mode="auto"``; for ``mode="manual"`` the
        admin's text is in ``original_name`` and ``title`` is ignored.
    mode:
        ``"auto"`` builds the stem from ``title`` and re-attaches meaningful
        source metadata (year, quality, edition, episode marker). ``"manual"``
        cleans the admin-supplied text.
    brand_rules:
        Iterable of ``(kind, pattern)`` pairs or rule objects.
    allowed_extensions:
        Optional iterable of permitted lower-case extensions; when given, an
        unknown extension is a hard error.
    """
    if mode not in {"auto", "manual"}:
        raise FilenameError(f"Unknown filename mode: {mode!r}")

    stem_raw, extension = split_extension(original_name)
    if not extension:
        raise FilenameError("The source filename has no extension.")
    if allowed_extensions is not None:
        allowed = {ext.lower().lstrip(".") for ext in allowed_extensions}
        if extension not in allowed:
            raise FilenameError(
                f"Extension {extension!r} is not allowed (allowed: {', '.join(sorted(allowed))})."
            )

    literals, regexes = _rule_tokens(brand_rules)
    drop = literals | _brand_tokens(brand_suffix)
    removed_seen: list[str] = []

    cleaned_raw = _strip_domains(_ascii(stem_raw), removed_seen)
    source_tokens, removed = _filter_tokens(
        _tokenise(cleaned_raw), drop=drop, regexes=regexes
    )
    removed_seen.extend(removed)

    if mode == "auto":
        if not (title or "").strip():
            raise FilenameError("Automatic mode needs a movie title.")
        title_clean = _strip_domains(_ascii(title), removed_seen)
        title_tokens, removed = _filter_tokens(
            _tokenise(title_clean), drop=drop, regexes=regexes
        )
        removed_seen.extend(removed)

        # Keep meaningful source metadata the title does not already contain.
        extra = []
        for token in source_tokens:
            if token in title_tokens or token in extra:
                continue
            if _is_preserved_metadata(token):
                extra.append(token)
        tokens = title_tokens + extra
    else:
        tokens = source_tokens

    if not tokens:
        raise FilenameError(
            "Nothing usable is left after removing branding and separators."
        )

    stem = _truncate_stem(_join(tokens), max_stem_length)
    if not stem:
        raise FilenameError("The cleaned filename is empty.")

    branded = _apply_suffix(stem, brand_suffix)
    # Final safety net: no path separators, no control characters, no spaces.
    branded = branded.replace("/", "-").replace("\\", "-")
    branded = re.sub(r"[\x00-\x1f\x7f]", "", branded)

    preserved = tuple(token for token in tokens if _is_preserved_metadata(token))
    return FilenameResult(
        filename=f"{branded}.{extension}",
        stem=branded,
        extension=extension,
        mode=mode,
        removed=tuple(dict.fromkeys(t for t in removed_seen if t)),  # de-dupe, keep order
        preserved=preserved,
    )


# Characters that are illegal on Windows/macOS filesystems. The branded display
# name keeps " | NZDWorld.com" exactly as specified; only the on-disk name is
# normalised, so a file can be copied off the server without breaking.
ILLEGAL_FILENAME_CHARS = '<>:"/\\|?*'
_RESERVED_STEMS = {
    "con", "prn", "aux", "nul",
    *{f"com{i}" for i in range(1, 10)},
    *{f"lpt{i}" for i in range(1, 10)},
}


def disk_safe_filename(filename: str, *, replacement: str = "-") -> str:
    """
    Convert a branded filename into a portable on-disk name.

    ``extraction-3-2026 | NZDWorld.com.mkv`` becomes
    ``extraction-3-2026 - NZDWorld.com.mkv`` (Windows cannot store ``|``).
    The displayed/downloaded name is unaffected.
    """
    name = (filename or "").strip()
    if not name:
        raise FilenameError("Empty filename.")

    for char in ILLEGAL_FILENAME_CHARS:
        name = name.replace(char, replacement)
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    # Windows strips trailing dots and spaces, which would silently rename files.
    name = name.rstrip(" .")
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    if stem.lower() in _RESERVED_STEMS:
        stem = f"{stem}_file"
    if not stem:
        raise FilenameError("Filename has no usable stem.")
    return f"{stem}.{ext}" if ext else stem


def preview_variants(
    original_name: str,
    title: str,
    *,
    manual_name: str = "",
    brand_rules=(),
    brand_suffix: str = DEFAULT_BRAND_SUFFIX,
    allowed_extensions=None,
) -> dict[str, str]:
    """
    Both proposals for the admin UI: ``{"auto": ..., "manual": ...}``.

    A failure in one mode never prevents showing the other; the error text is
    returned in ``errors`` instead of raising.
    """
    out: dict[str, str] = {"auto": "", "manual": "", "errors": ""}
    errors: list[str] = []
    try:
        out["auto"] = clean_filename(
            original_name,
            title=title,
            mode="auto",
            brand_rules=brand_rules,
            brand_suffix=brand_suffix,
            allowed_extensions=allowed_extensions,
        ).filename
    except FilenameError as exc:
        errors.append(f"auto: {exc}")
    if manual_name:
        try:
            out["manual"] = clean_filename(
                manual_name,
                mode="manual",
                brand_rules=brand_rules,
                brand_suffix=brand_suffix,
                allowed_extensions=allowed_extensions,
            ).filename
        except FilenameError as exc:
            errors.append(f"manual: {exc}")
    out["errors"] = "; ".join(errors)
    return out
