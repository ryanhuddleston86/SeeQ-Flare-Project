# Capsule Reconciler — Split Provenance Design

Pre-implementation walkthrough for the split metadata / provenance marker
decisions. Ryan to confirm before the test harness is updated.

---

## Decision 1: Where the provenance marker lives

Flat fields on `metadata`, not nested:

```python
# Primary fragment — keeps original UUID, annotation is "authentic" for this fragment.
# No note_origin here: it IS the original holder.
Capsule(id="uuid-A", metadata={
    "annotation": "Long outage",
    "split_warning": True,
})

# Secondary fragment(s) — new UUID, full copy of annotation + explicit provenance.
Capsule(id="uuid-B", metadata={
    "annotation": "Long outage",
    "note_origin": "split_from:uuid-A",   # ← the marker
    "split_warning": True,
})
```

`note_origin` value is always the string `"split_from:<original_capsule_id>"` — the UUID
of the capsule that existed *before* the split. An auditor (or later code) can read either
fragment and know exactly where the note came from.

---

## Decision 2: Editing divergence — what happens to `note_origin`

The spike won't implement an edit flow, but the rule baked in (via comment and test fixture) is:

**A user edit clears `note_origin`.** Once someone independently writes about a fragment,
the note is original to that fragment. Keeping `note_origin` after a user edit would mean
"this note is a copy" — which is no longer true. The test will simulate this by directly
mutating the metadata (clearing `note_origin` + updating `annotation`), which mirrors what
a real edit path would do.

---

## Decision 3: Merge deduplication shape

When merging capsules into `merged_from`, the dedup check compares **annotation text only**
(stripped), not the full metadata dict:

```python
seen_annotations = set()
merged_from_entries = []
for cap in sorted_source_capsules:
    text = (cap.metadata.get("annotation") or "").strip()
    if text and text in seen_annotations:
        continue   # exact duplicate — skip
    if text:
        seen_annotations.add(text)
    merged_from_entries.append({"id": cap.id, **cap.metadata})
```

"Near-exact" matching (typo-level similarity) is out of scope for the spike — exact
stripped-string match only. A comment in code will note that production should use a
similarity threshold.

One consequence worth flagging: if Fragment P still has `note_origin: "split_from:uuid-A"`
when it gets merged (i.e., it was *never* independently edited), the dedup check will still
see the same annotation text from its sibling and drop the duplicate. The `note_origin`
field on the surviving entry in `merged_from` tells the full story.

---

## Open questions — confirm before implementation

| # | Question | Options |
|---|---|---|
| 1 | **Marker shape** | Flat `note_origin` string on `metadata` as shown above — OK? |
| 2 | **Primary vs secondary asymmetry** | Primary fragment (original UUID) gets no `note_origin`, secondary fragments do. OK — or do you want `note_origin` on *all* fragments including the primary (e.g. `note_origin: "original"`) to make provenance fully symmetric? |
| 3 | **Edit-clears-marker rule** | Does clearing `note_origin` on edit belong in the spike comment/test, or is that out of scope for now? |
