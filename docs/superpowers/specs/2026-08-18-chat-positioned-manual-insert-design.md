# Positioned Manual Text Insertion Through Chat

## Goal

Allow users to add authored text through chat at a predictable visual position without source grounding. The inserted content is always a new paragraph and never modifies the target block's existing text.

## Supported Requests

The deterministic parser recognizes manual insertion commands beginning with Russian forms such as `добавь`, `добавить`, `вставь`, `вставить`, `допиши`, and `дописать`.

Supported positions:

- `в начало документа` inserts the paragraph at the first document position.
- `в конец документа` inserts the paragraph at the last document position.
- `в начало второго абзаца` and `перед вторым абзацем` insert before the second visual text unit.
- `в конец второго абзаца` and `после второго абзаца` insert after the second visual text unit.

Ordinal references support arbitrary positive digits (`2`, `21`, `2-го`) and Russian ordinal words from `первого` through `двадцатого`, including the grammatical forms used after `перед`. Matching is case-insensitive, and punctuation between the position and authored text is removed.

## Visual Paragraph Numbering

Numbering follows the rendered document order rather than only `paragraph` nodes. The following count as visual paragraphs:

- heading nodes;
- paragraph nodes;
- each individual item inside a list node.

Tables, images, gaps, and empty non-text blocks are excluded. Nested document nodes are traversed in rendered order.

## List Item Targets

When the target is a list item, the inserted content remains a normal paragraph. To place it at the requested visual position, the list node is split around the target boundary:

- items before the insertion remain in the original list segment;
- the new paragraph is inserted between list segments;
- items after the insertion form a second list segment;
- `items`, `items_html`, and `item_styles` are split at the same boundary;
- other list-level data, including ordering and style, is copied to both segments;
- empty list segments are omitted.

For an insertion before the first item or after the last item, a split is unnecessary when the paragraph can be placed directly before or after the list node.

## Data Flow

1. Detect an explicit manual insertion command before relying on the language model's edit plan.
2. Extract authored text and the requested position.
3. Resolve the position against the current `WorkingDocument` using visual paragraph numbering.
4. Build deterministic document operations: a direct `InsertNode` for ordinary boundaries, or list update/split operations for an internal list boundary.
5. Apply operations through the existing `DocumentEditService` revision flow.
6. Mark every inserted paragraph with `manual-edit` and return `Добавлен текст пользователя`.

The authored text bypasses source-grounding validation. Other model-generated operations keep their existing grounding behavior.

## Error Handling

If an explicit ordinal target does not exist, no document change is applied. The chat returns a clear message that the requested paragraph was not found. Empty authored text is rejected rather than inserting an empty paragraph.

If the model fails to return a structured response but the manual command is valid and resolvable, the deterministic manual insertion still proceeds.

## Testing

Service tests cover:

- insertion at document start and end;
- insertion before and after an ordinary paragraph;
- headings participating in numbering;
- every list item participating separately in numbering;
- insertion before and after an internal list item with metadata preserved;
- numeric and Russian ordinal references;
- missing target and empty authored text;
- fallback behavior when the model returns no operations or raises a structured-response error;
- regression coverage for the existing question-and-answer text normalization.
