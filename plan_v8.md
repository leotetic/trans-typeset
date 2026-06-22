**当前现状**
核心 schema 在 [models.py](/Users/leotetic/app/trans-typesetting/packages/schema/pdf_translator_schema/models.py:195) 已有：

`UserIntent`
记录 `target_lang`、`output_kind`、`style_intent`、`typesetting_standard`、自然语言 `instruction`、保留策略、基础 constraints、单双栏。

`SemanticLayoutAnalysis`
记录 block role candidates、section hints、asset usage hints、confidence、quality flags。

`LayoutIntentPlan`
记录 block 级角色、priority、render_intent、asset usage、column_layout。

`RenderDefaults`
记录字体栈、行高、段距、页面尺寸/页边距、角色样式、overflow policy、公式编号等。

后端已经有 LangGraph 节点链路，在 [nodes.py](/Users/leotetic/app/trans-typesetting/services/api/app/pipeline/agents/nodes.py:475) 中执行：

`read_input -> analyze_intent -> semantic_recognize -> build_plan -> validate_plan -> translate -> render -> evaluate_render -> repair -> export_pdf -> complete`

LangChain structured output 在 [llm.py](/Users/leotetic/app/trans-typesetting/services/api/app/pipeline/agents/llm.py:30) 直接输出 `UserIntent`、`SemanticLayoutAnalysis`、`LayoutIntentPlan`。无 key 时 deterministic fallback 仍可跑。

**目标**
用自然语言作为提示词输入，agent 能识别语义，转化为schema 最终交付给 renderer，最终升级成“通用学术文档智能排版”agent。

**主要差距**
你的目标 schema 与当前 schema 的差距如下：

1. 任务意图
当前只有 `OutputKind` 和 `StyleIntent`，没有“课程论文、本科论文、实验报告、开题报告、普通作业”等强类型任务。也没有 `output_formats=["docx","pdf"]`，目前后端只导出 PDF，前端下载也只认 PDF。

2. 论文结构
当前 `BlockRole` 有 title、abstract、heading、paragraph、caption、formula、table、figure、footnote、reference，但没有 keywords、toc、acknowledgements、appendix、cover、author info、department、course info、experiment metadata 等论文/报告结构。也没有“章节树”或“文档结构计划”，只是 block 列表。

3. 模板规则
当前只有 `typesetting_standard=gb_t_7713_1_2025|none` 和 `layout_reference` 输入源，没有学校模板、课程要求、用户自定义规则、默认模板 fallback 的结构化表达。

4. 页面设置
当前 `PageLayoutDefaults` 有尺寸和页边距，但缺少纸张枚举、方向、页眉页脚、页码格式、分节、分页规则、封面/目录/正文不同 section 设置。

5. 样式体系
当前 `RoleStyles` 已覆盖 title、abstract、heading、paragraph、caption、formula、table、figure、footnote、reference，但不支持多级标题、目录样式、关键词、附录、致谢、封面字段、参考文献悬挂缩进等细粒度学术样式。

6. 编号与目录
当前只有 `formula_numbering`，没有 heading numbering、figure/table numbering、reference numbering、目录生成、目录层级、编号样式。

7. 参考文献规则
当前 parser 能识别 reference block，preserve token 能保留 reference markers，但 schema 没有 `citation_style=gb_t_7714|apa|mla|...`、语言/场景默认规则、正文引用与 bibliography 的校验策略。

**目标 Schema 形态建议**
建议新增或扩展为 v0.2，不要把所有东西塞进现有 `LayoutIntentPlan.blocks[]`。推荐结构：

```text
UserIntent
  -> task_intent
  -> output_targets
  -> template_profile
  -> reference_style

SemanticLayoutAnalysis
  -> document_structure candidates
  -> block-to-section mapping
  -> missing/uncertain sections

LayoutIntentPlan
  -> document_profile
  -> structure_plan
  -> page_setup
  -> style_system
  -> numbering_plan
  -> bibliography_plan
  -> blocks/assets
  -> quality_flags

RenderDefaults
  -> deterministic renderer defaults derived from plan
```

具体模型建议：

`TaskIntent`
包含 `document_kind`: `course_paper | undergraduate_thesis | lab_report | proposal_report | homework | generic_academic`，`audience`，`language`，`confidence`，`evidence`。

`OutputTarget`
包含 `format`: `docx | pdf | html_preview`，`required`，`artifact_name`。默认应是 `html_preview + pdf`，当用户目标是论文排版时支持 `docx + pdf`。

`TemplateProfile`
包含 `source`: `school_template | course_requirement | user_specified | default_academic`，`standard`，`institution`，`department`，`template_asset_ids`，`fallback_used`。

`DocumentStructurePlan`
包含有序 sections：`cover/title/abstract/keywords/toc/body/heading/figure/table/formula/references/appendix/acknowledgements`，每个 section 有 `section_id`、`kind`、`title`、`level`、`source_block_ids`、`required`、`quality_flags`。

`PageSetup`
包含 `paper_size`、`orientation`、`margins`、`header_footer`、`page_numbering`、`section_breaks`、`page_breaks`。注意这里仍是语义规则，不给 LLM 坐标。

`StyleSystem`
从当前 `RoleStyles` 升级为命名样式：`paper_title`、`heading_1`、`heading_2`、`body`、`abstract`、`keywords`、`caption_figure`、`caption_table`、`reference_entry`、`toc_entry` 等。

`NumberingPlan`
包含 `heading_numbering`、`figure_numbering`、`table_numbering`、`formula_numbering`、`reference_numbering`、`toc_generation`。

`BibliographyPlan`
包含 `citation_style`: `gb_t_7714 | apa | mla | ieee | chicago | auto`，`default_reason`，`in_text_citation_policy`，`bibliography_sorting`，`hanging_indent`。

**实施计划**
1. Schema Agent 先落 v0.2 contract
修改 `packages/schema/pdf_translator_schema/models.py`、`defaults.py`、`typescript/src/index.ts`、`json_schema.py`、`packages/json-schema/*.json`、`docs/schema.md`。
保持现有 v0.1 字段兼容，新增字段默认值，避免一次性打断当前 PDF 翻译路径。

2. 先扩 `UserIntent`
加入 `task_intent`、`output_targets`、`template_profile`、`bibliography_preference`。
在 `coerce_user_intent` 中做 deterministic keyword fallback：课程论文、本科论文、实验报告、开题报告、普通作业、Word、PDF、GB/T 7714、APA、MLA、学校模板等。

3. 扩 `SemanticLayoutAnalysis`
让 agent 输出结构候选和 block-to-section 信号，而不只是 role candidates。
例如识别“摘要/关键词/目录/参考文献/附录/致谢/实验目的/实验原理/实验步骤/结果分析”。

4. 扩 `LayoutIntentPlan` 为文档级计划
新增 `structure_plan`、`page_setup`、`style_system`、`numbering_plan`、`bibliography_plan`。
`blocks[]` 继续保留 source block 映射，renderer 仍用它做逐块消费。

5. 加验证规则
`validate_layout_intent_plan` 应校验：所有 source block 被覆盖；section 引用的 block 必须存在；编号/目录规则不能引用不存在 section；LLM 仍不得返回坐标字段；要求 Word/PDF 时必须有对应 output target。

6. Renderer 分两步消费
第一步只让 HTML/PDF renderer 消费 `page_setup/style_system/numbering_plan` 的安全子集：页边距、标题层级样式、图表/公式编号、目录占位。
第二步新增 DOCX exporter。建议用独立 `packages/renderer/docx_renderer`，共享 `RenderDocument` 或新增 `TypesetDocument` 中间模型，避免把 Word 逻辑塞进 PDF renderer。

7. 后端 artifact/export 扩展
把 `export_pdf` 改成 `export_artifacts`，支持 `translated.pdf`、`translated.docx`。
API 增加 `/download/pdf`、`/download/docx` 或 artifact 列表下载；保留旧 `/download` 指向 PDF 兼容当前前端。

8. 前端最小适配
上传页自然语言 instruction 继续作为主入口。
增加输出格式选择，默认 PDF；目标论文排版场景可勾选 Word。
完成后展示 PDF/DOCX 两个下载按钮，schema inspector 增加 intent、structure、numbering、bibliography 展示。

9. 测试顺序
先 schema 单测，再 workflow deterministic 单测，再 renderer 单测，最后 API 和前端 typecheck/build。
重点 fixture：本科论文、实验报告、开题报告、普通作业、明确 APA、无格式要求 fallback 默认学术论文、要求 Word+PDF。

**建议分期**
Phase A：schema v0.2 和 deterministic intent 识别，不改渲染结果。
Phase B：agent structured output 识别任务类型、结构、模板、编号、参考文献规则。
Phase C：renderer 消费页面/样式/编号/目录的 PDF 子集。
Phase D：DOCX exporter 和多 artifact 下载。
Phase E：学校模板/课程模板解析与更完整的结构补全。

---

**本轮优化完成内容（兼容式 v0.2 Contract）**

本轮按 Phase A/B 的兼容范围落地，不改变当前 PDF 预览/下载主路径，不新增 DOCX exporter。

1. Schema contract
   - `UserIntent` 默认升级为 `schema_version="0.2"`，新增 `task_intent`、`output_targets`、`template_profile`、`bibliography_preference`。
   - `SemanticLayoutAnalysis` 默认升级为 `0.2`，新增 `structure_candidates`、`block_section_mappings`、`missing_sections`、`uncertain_sections`。
   - `LayoutIntentPlan` 默认升级为 `0.2`，新增 `document_profile`、`structure_plan`、`page_setup`、`style_system`、`numbering_plan`、`bibliography_plan`。
   - 新增文档类型、输出格式、模板来源、section kind、纸张/方向、编号样式、引用格式等枚举和模型。
   - 旧 `schema_version="0.1"` payload 仍可 validate，新字段均提供默认值。

2. Deterministic agent fallback
   - `coerce_user_intent` 可识别本科论文、课程论文、实验报告、开题报告、普通作业。
   - 可识别 Word/DOCX、PDF、学校模板、课程要求、用户自定义模板、GB/T 7714、APA、MLA、IEEE、Chicago。
   - 默认输出目标为 `html_preview + pdf`；明确要求 Word/DOCX 时只记录 `docx` output target，不生成 docx 文件。
   - deterministic semantic/layout plan 会为 block 生成 section candidate、block-to-section mapping、structure plan、numbering plan 和 bibliography plan。

3. Validation and safety
   - `validate_layout_intent_plan` 会校验 structure section 引用的 block 必须存在。
   - 校验 numbering、toc 和 bibliography 引用的 section 必须存在。
   - 新增 v0.2 嵌套模型继承无坐标约束，继续拒绝 `bbox/x/y/page/width/height` 等布局字段。

4. Contract sync and docs
   - 同步 Python schema、TypeScript types、JSON Schema 导出和 `docs/schema.md`。
   - LangChain structured output prompt 已更新为 v0.2 intent/semantic/layout plan。
   - 增加 schema、agent loop、orchestrator deterministic path 测试。

**本轮验证**

已执行：

```bash
.venv/bin/python -m pytest packages/schema/tests
.venv/bin/python -m pytest services/api/tests/test_agent_loop.py
.venv/bin/python -m pytest services/api/tests/test_orchestrator.py
.venv/bin/python -m compileall packages services
npm run typecheck:web
.venv/bin/python -m pytest
npm run build:web
```

**下一步优化方向**

1. Renderer 消费 v0.2 安全子集
   - 让 HTML/PDF renderer 读取 `structure_plan`、`page_setup`、`style_system`、`numbering_plan` 的安全字段。
   - 先支持标题层级、目录占位、图表/公式/参考文献编号、关键词/致谢/附录样式。

2. Artifact 和下载链路扩展
   - 将 `export_pdf` 演进为 `export_artifacts`。
   - 增加 artifact 列表里的 PDF/DOCX/HTML preview 下载目标。
   - 保留旧 `/download` 指向 PDF 的兼容行为。

3. DOCX exporter
   - 新增独立 DOCX renderer/exporter，优先共享 schema 级 `TypesetDocument` 或 renderer 中间模型。
   - 不把 Word 逻辑直接塞进当前 PDF renderer。

4. 学校/课程模板解析
   - 支持上传学校模板或课程要求文档，抽取 `TemplateProfile`、命名样式、页眉页脚、目录和参考文献规则。
   - 将 deterministic keyword fallback 升级为 template-aware resolver。

5. 更完整的结构补全与诊断
   - 对缺失摘要、关键词、目录、参考文献、实验报告必要 section 输出更明确的质量 flags。
   - 前端 schema inspector 可突出展示 intent、structure、numbering、bibliography，而不是只显示原始 JSON。
