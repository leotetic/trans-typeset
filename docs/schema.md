# Schema v0.1

`TranslationLayoutPlan@0.1` 是给大模型看的唯一输出 contract。它刻意停留在文本语义层，不允许返回绝对坐标。

## LLM 输入

后端发送 `TranslationChunk`，每个 block 包含：

- `block_id`: renderer 用来把译文放回原始 `DocumentIR` block。
- `role`: 标题、摘要、正文、图注、公式、参考文献等语义角色。
- `source_text`: 原文。
- `nearby_titles`: 局部上下文。
- `preserve_tokens`: 必须保留的 citation、公式、reference marker、figure/table token。

## LLM 输出

模型必须返回：

- `schema_version: "0.1"`
- `chunk_id`
- `target_lang`
- `blocks[]`

每个 `blocks[]` 元素只允许包含：

- `source_block_id`
- `translated_text`
- `inline_items`
- `role`
- `render_intent`
- `quality_flags`

renderer 负责坐标、分页、溢出、字号缩放和 continuation page。模型不得返回 `bbox`、`x`、`y`、`page` 等布局坐标字段；Pydantic models 使用 `extra="forbid"` 拒绝这些字段。

## 默认排版值

- `target_lang`: `zh-CN`
- `font_stack`: `Noto Sans CJK SC`, `Source Han Sans SC`, `Arial Unicode MS`, `sans-serif`
- `line_height`: `1.35`
- `paragraph_spacing_em`: `0.45`
- `overflow_policy.strategy`: `scale_then_expand_then_continue`
- `overflow_policy.min_font_scale`: `0.86`

