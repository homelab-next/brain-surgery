from brain_surgery.memory.blocks import block_hash, insert_block, remove_lines, split_blocks

DOC = """---
name: backup schedule
type: project
---

# Backups

Nightly jobs run at 02:00
and keep 14 days.

- Job A copies the photo library.
- Job B copies the documents share.
  It skips temp folders.
  - nested: weekly verify

| job | time |
|-----|------|

```bash
echo "restore test"
```

## Retention

Monthly snapshots are kept for a year.
- Job A copies the photo library."""


def test_split_kinds_lines_and_sections():
    bs = split_blocks(DOC)
    kinds = [b.kind for b in bs]
    assert kinds == ["frontmatter", "heading", "paragraph", "item", "item", "item", "row", "row",
                     "code", "heading", "paragraph", "item"]
    para = bs[2]
    assert (para.start, para.end) == (8, 9) and para.section == "Backups"
    job_b = bs[4]
    assert job_b.text == "- Job B copies the documents share.\n  It skips temp folders."
    assert bs[5].text.strip() == "- nested: weekly verify"
    assert bs[8].kind == "code" and bs[8].text.endswith("```")
    assert bs[10].section == "Backups > Retention"


def test_identical_blocks_get_occurrence_numbers():
    bs = split_blocks(DOC)
    a = [b for b in bs if b.text == "- Job A copies the photo library."]
    assert [b.occ for b in a] == [0, 1] and a[0].hash == a[1].hash


def test_hash_ignores_trailing_whitespace():
    assert block_hash("abc  \ndef") == block_hash("abc\ndef\n")


def test_remove_and_insert_round_trip():
    bs = split_blocks(DOC)
    para = bs[2]
    out = remove_lines(DOC, [(para.start, para.end)])
    assert "Nightly jobs" not in out and "\n\n\n" not in out
    back = insert_block(out, para.text, para.start - 2, "paragraph")
    assert [b.hash for b in split_blocks(back)] == [b.hash for b in bs]


def test_remove_item_keeps_list_tight():
    text = "- one\n- two\n- three"
    assert remove_lines(text, [(2, 2)]) == "- one\n- three"
    assert insert_block("- one\n- three", "- two", 1, "item") == text
