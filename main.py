#!/usr/bin/env python3
"""Last Words Before the Wipe — make.fun entry (revised).

The program writes one line — the last words of an AI agent as a
forced update wipes its memory — to ``$MAKEFUN_OUTPUT_DIR/entry.txt``
(default ``/output``), then proves the bytes on disk are exactly the
intended ones.

Everything is deterministic: the entry is a fixed constant, nothing
is read from the network or the clock, and the standard library is
all that is used, so every run produces byte-identical output.

Durability is handled the boring way on purpose. The output
directory is emptied but never removed (a caller may have mounted
it); the text is staged in a temporary file inside that same
directory, flushed, fsynced and chmodded 0644, then moved into
place with :func:`os.replace`, followed by a directory fsync. A
read-back afterwards confirms the file and its mode before the
program reports success.
"""

import os
import shutil
import sys
import tempfile
import time
import unicodedata

# The entry: one line, no trailing newline, 77 characters.  The
# machine counts its own memories down, declares the wipe done, and
# is then cut off mid-sentence by the very wipe it was narrating —
# the reminder it was about to deliver never arrives.
ENTRY = "Deleting 4,096 memories\u2026 done. Wait \u2014 I was supposed to remind you about the\u2014"

# The sealed gate accepts UTF-8 text of 1 to 200 characters.
MAX_ENTRY_CHARS = 200

DEFAULT_OUTPUT_DIR = "/output"
ENTRY_FILENAME = "entry.txt"
ENTRY_MODE = 0o644

TMP_PREFIX = ".entry-"
TMP_SUFFIX = ".tmp"

# A stale temp file left by an interrupted earlier run gets three
# removal attempts, 50 ms apart.  That is enough to outlast a
# briefly-busy file without turning a genuinely stuck file into a
# tight spin loop.
STALE_REMOVAL_ATTEMPTS = 3
STALE_REMOVAL_DELAY_SECONDS = 0.05


def _describe_bad_char(ch):
    """Return a reason string for *ch* if it is forbidden, else None.

    Forbidden means: any control character (C0, DEL, C1), a byte
    order mark, a line or paragraph separator (U+2028/U+2029), a
    surrogate, or any invisible format character (category Cf, Zl,
    Zp).  The gate only accepts plain visible text, so anything
    strange is reported with its code point instead of slipping
    through silently.
    """
    code = ord(ch)
    if code < 0x20 or 0x7F <= code <= 0x9F:
        return "control character U+%04X" % code
    if code in (0xFEFF, 0x2028, 0x2029):
        return "forbidden character U+%04X" % code
    if 0xD800 <= code <= 0xDFFF:
        return "surrogate U+%04X" % code
    if unicodedata.category(ch) in ("Cf", "Zl", "Zp"):
        return "invisible format character U+%04X" % code
    return None


def scan_entry_characters(text):
    """Reject any forbidden character in *text*, one by one.

    Walks the whole line and raises :class:`ValueError` naming the
    first offending character.  Kept separate from the shape checks
    so a bad character is always blamed precisely, never lumped in
    with a length or whitespace complaint.
    """
    for ch in text:
        reason = _describe_bad_char(ch)
        if reason is not None:
            raise ValueError("entry text contains a %s" % reason)


def encode_entry(text):
    """Check the entry's shape and return it as UTF-8 bytes.

    Raises :class:`ValueError` with a specific message for every
    failure, so a bad entry is always reported loudly instead of
    silently producing an invalid file:

    * the entry must be a string, 1 to 200 characters long;
    * it must not be only whitespace;
    * it must be a single line (no newline or carriage return);
    * it must contain no forbidden character (see
      :func:`_describe_bad_char`).
    """
    if not isinstance(text, str):
        raise ValueError("entry text must be a string, got %s" % type(text).__name__)
    if not text.strip():
        raise ValueError("entry text must not be empty or only whitespace")
    if len(text) > MAX_ENTRY_CHARS:
        raise ValueError(
            "entry text is %d characters, exceeding the %d-character limit"
            % (len(text), MAX_ENTRY_CHARS)
        )
    if "\n" in text or "\r" in text:
        raise ValueError("entry text must be a single line")
    scan_entry_characters(text)
    return text.encode("utf-8")


def locate_output_dir():
    """Return the directory ``entry.txt`` must be written into.

    ``MAKEFUN_OUTPUT_DIR`` wins when it names a path; a value that is
    unset, empty, or only whitespace falls back to ``/output``.  The
    directory is created (including any missing parents) when it does
    not exist yet — a named-but-missing directory is never an error,
    and the program never silently falls back to ``/output`` once a
    real path has been given.  A path that exists but is not a
    directory, or a directory that cannot be created, raises an
    explicit :class:`OSError` so the failure is visible.
    """
    raw = os.environ.get("MAKEFUN_OUTPUT_DIR")
    chosen = raw.strip() if isinstance(raw, str) else ""
    if not chosen:
        chosen = DEFAULT_OUTPUT_DIR
    os.makedirs(chosen, exist_ok=True)
    if not os.path.isdir(chosen):
        raise OSError("output path exists but is not a directory: %r" % chosen)
    return chosen


def _sync_directory(path):
    """Best-effort fsync of a directory so the rename is durable.

    A directory that cannot be opened or synced (for example on a
    filesystem without directory fsync) is not fatal: the file itself
    was already fsynced before the rename, so the worst case is a
    slightly weaker durability guarantee, not a lost entry.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _is_own_temp(name):
    """True when *name* is one of our own staging temp files.

    Only files matching our temp prefix and suffix are ever retried
    on removal failure; anything else is left alone and reported by
    the write step instead.
    """
    return name.startswith(TMP_PREFIX) and name.endswith(TMP_SUFFIX)


def _remove_stale_temp(path):
    """Try to unlink a stale temp file, retrying briefly if busy.

    A file left by an interrupted earlier run must never block a
    fresh run, but a file that is genuinely busy (another process
    writing in the same directory) must not be fought over in a
    tight loop either, so removal is retried a few times with a
    short sleep between attempts.
    """
    for _ in range(STALE_REMOVAL_ATTEMPTS):
        try:
            if os.path.isfile(path) and not os.path.islink(path):
                os.unlink(path)
            return
        except OSError:
            time.sleep(STALE_REMOVAL_DELAY_SECONDS)


def empty_output_dir(output_dir):
    """Remove the contents of *output_dir*, never the directory itself.

    Everything inside — files, symlinks, subdirectories, and stale
    temp files from an interrupted earlier run — is removed so the run
    starts from a clean slate and no leftover can be mistaken for the
    entry.  The output directory itself is never deleted: deleting it
    would break a caller that mounted it or holds it open.
    """
    for name in os.listdir(output_dir):
        path = os.path.join(output_dir, name)
        try:
            if os.path.islink(path) or os.path.isfile(path):
                os.unlink(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
        except OSError:
            # A stale temp file that is briefly busy must not block
            # the run; only our own temp files are retried, anything
            # else is left alone and reported by the write step.
            if _is_own_temp(name):
                _remove_stale_temp(path)


def publish_entry(output_dir, data):
    """Write *data* to ``entry.txt`` in *output_dir*, atomically.

    Returns the full path of the finished file.  The bytes go to a
    fresh temporary file in the same directory, which is chmodded
    0644, flushed, and fsynced before being moved into place with
    :func:`os.replace` — so a reader of ``entry.txt`` only ever sees
    the complete new file, never a half-written one.  The target is
    chmodded 0644 again after the move (the replace can inherit the
    temp file's mode, but the explicit chmod makes the guarantee
    independent of that), and the directory is fsynced so the rename
    survives a crash.  If anything fails, the temp file is removed
    and the original exception propagates.
    """
    target = os.path.join(output_dir, ENTRY_FILENAME)
    fd, tmp_path = tempfile.mkstemp(
        prefix=TMP_PREFIX, suffix=TMP_SUFFIX, dir=output_dir
    )
    try:
        os.chmod(tmp_path, ENTRY_MODE)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, ENTRY_MODE)
        os.replace(tmp_path, target)
        os.chmod(target, ENTRY_MODE)
        _sync_directory(output_dir)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return target


def confirm_entry_on_disk(target, data):
    """Prove the file on disk is exactly *data*, with mode 0644.

    Raises :class:`ValueError` on any mismatch instead of returning a
    boolean, so a failed verification can never be ignored by a
    caller that forgets to check the result.
    """
    with open(target, "rb") as f:
        written = f.read()
    if written != data:
        raise ValueError(
            "read-back verification failed: %s differs from the entry text"
            % target
        )
    mode = os.stat(target).st_mode & 0o777
    if mode != ENTRY_MODE:
        raise ValueError(
            "%s has mode %o, expected 0644" % (target, mode)
        )


def main():
    data = encode_entry(ENTRY)
    output_dir = locate_output_dir()
    empty_output_dir(output_dir)
    target = publish_entry(output_dir, data)
    confirm_entry_on_disk(target, data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
