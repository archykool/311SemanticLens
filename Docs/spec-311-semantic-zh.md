# NYC 311 语义治理基础设施 · 项目规格书 (Spec)

**项目代号**:Rechannel(暂定,待确认)
**版本**:v0.1 草案
**日期**:2026-07-02
**作者**:Archy

---

## 1. 项目概述

对 NYC 311 投诉数据构建一个语义检索与信号分析原型。核心主张:311 现有分类体系(191 个 top category / 951 个 sub category)是单标签、以部门为中心的,而现实城市问题是多面向、跨部门的(例:trash-clogged catch basin 同时涉及 DSNY、DEP、DOT)。本系统通过 LLM 批量语义增强,将投诉记录重新映射到多维 ontology,并提供自然语言 hybrid 检索(BM25 + 向量)与聚合分析。

### 1.1 双重目标(同等优先级)

| 目标 | 受众 | 成功形态 |
|---|---|---|
| 求职作品集 | 招聘方 / 面试官 | 展示 Elasticsearch、Docker、Postgres、评估方法论等高频技能;README 有清晰的工程决策叙事 |
| 政府侧演示 | City Council chief data scientist | 展示跨部门信号发现能力;能回答政府式自然语言问题 |

### 1.2 设计原则(来自 Instacart 案例的反向推理)

- 本系统 workload 为 **read-heavy、append-only**(每天约 8k–10k 条新增,老记录几乎不改),与 Instacart 的写入灾难场景相反,因此 Elasticsearch 是正当选择。
- 架构纪律:**Postgres 是唯一事实源(system of record),ES 是可全量重建的派生数据(derived data)**。同步为单向 batch,不存在双写一致性问题。
- **零训练**:embedding 模型为现成预训练模型,LLM 标注走 API。所有投入为设计与工程时间。

---

## 2. 系统总览

```
[离线 batch]  Socrata API → Postgres(事实源)→ 语义增强(LLM)+ 向量化 → 构建 ES index
[在线 serving] 浏览器 → FastAPI(query 理解 + query 向量化)→ ES hybrid 检索(BM25 + kNN, RRF 融合)→ 聚合 → JSON
[离线评估]   FAISS IndexFlatIP 暴力精确 top-k 作为 ground truth,测量 ES 近似检索的 recall@k
```

技术栈:Elasticsearch 8.x(serving)、PostgreSQL 16(事实源)、FAISS(仅评估)、FastAPI(服务层)、bge-small-en-v1.5 或 e5-base(384 维 embedding)、Docker Compose(一键编排)。

---

## 3. 组件规格

以下每个组件以 {输入 / 输出 / 边界 / 验收标准} 界定。边界为实线:边界之外的事项本组件明确不做。

### C1 · 数据拉取(Ingestion)

- **输入**:NYC Open Data 311 数据集(Socrata API);阶段一范围为 catch-basin 相关切片(约 766K 行 / 460MB),阶段二为 2025 全年(约 3.4M 行)。
- **输出**:Postgres 中的原始记录表(保留 unique key、created/closed 时间、complaint_type、descriptor、resolution_description、geo 字段、agency、status),含增量拉取水位标记。
- **边界**:不做实时流式接入;不拉取非公开字段(通话录音、个人信息);不在拉取阶段做任何清洗之外的语义处理;全量历史(21M 行)不在原型范围内,仅列入 roadmap。
- **验收标准**:(1) 单命令可从零拉全阶段一切片并可断点续跑;(2) 重复运行幂等,行数与 Socrata 端计数误差 < 0.1%;(3) 增量模式下每日新增可在 10 分钟内同步完成。

### C2 · 语义增强层(Ontology + LLM Enrichment)

- **输入**:Postgres 中 unique 的 (top category × sub category × descriptor) 组合(量级为数千个 unique 模式,而非逐行记录);人工设计的多维 ontology(facet 维度草案:设施类型、失效模式、涉及部门、空间尺度,可增删)。
- **输出**:Postgres 中的映射表:每个 unique 组合 → 多标签 facet 集合 + 跨部门归属集合;ontology 定义文件(版本化,视同代码管理)。
- **边界**:不对 21M/766K 条记录逐条调用 LLM;不训练或微调任何模型;ontology 维度数由问题集反推,不追求"完备分类学";resolution_description 的逐条语义分析为可选扩展,不在阶段一。
- **验收标准**:(1) enrichment 一次性 API 成本 < $20;(2) 抽样 100 个组合人工复核,多标签映射准确率 ≥ 90%;(3) trash-clogged catch basin 类投诉被正确映射到 ≥ 2 个部门 facet;(4) ontology 文件有版本号与变更记录。

### C3 · Embedding 管道

- **输入**:每条记录拼接文本(category + sub category + descriptor,约 30–60 token);预训练模型 bge-small-en-v1.5(384 维)。
- **输出**:与记录一一对应的 384 维向量,先落 Postgres(含处理进度检查点),再由 C4 消费。
- **边界**:document embedding 与 query embedding 必须使用同一模型(同一向量空间);不做模型微调;不做多模型 ensemble;阶段一向量总量约 1.2GB(766K),阶段二约 5GB(3.4M),21M 全量不在范围内。
- **验收标准**:(1) 每批 256–1024 条,任务可断点续跑且幂等;(2) 766K 切片在单机 CPU 上 ≤ 2 小时完成(或 GPU ≤ 30 分钟);(3) 向量行数与记录行数严格一致。

### C4 · ES 索引构建

- **输入**:Postgres 中的记录 + facet 映射 + 向量。
- **输出**:单一 ES index,mapping 含:全文字段(tsvector 语义的 text 字段,BM25)、dense_vector(HNSW,可开 int8 量化)、结构化 filter 字段(borough、community district、时间、agency、facets、status)。
- **边界**:ES 中不存在任何"仅存在于 ES"的数据——index 必须可随时从 Postgres 全量重建;不做多 index 分片策略(单 index 足够覆盖阶段二量级);不做 ES 集群,高可用不在原型范围。
- **验收标准**:(1) 从空 ES 重建阶段一 index ≤ 30 分钟;(2) 重建后文档数与 Postgres 严格一致;(3) 内存占用在 16GB 单机内(必要时开 int8 量化)。

### C5 · Query Understanding(FastAPI 内)

- **输入**:用户自然语言查询(中文/英文);预定义的 chief-DS 问题集(已定,见下)。
- **输出**:结构化 query 对象 `{topic(语义部分), geo, time_range, aggregation}`——四维经问题集反推确认够用;`time_range` 为**可缺省**属性;`aggregation` 需支持 **drill-down**(先分组统计再展开明细),非扁平枚举。

**chief-DS 问题集(D1 产出,已锁定):**

| # | 问题 | 主打能力 | topic | geo | time | aggregation |
|---|---|---|---|---|---|---|
| Q1 头号 | "雨水排不出去的问题,布鲁克林哪些社区最严重?" | 语义理解 | ✓(字面不含类目词) | Brooklyn,按 community district 分组 | 缺省 | 类目分布 → 钻取记录(drill-down) |
| Q2 | 一条 catch basin 投诉其实牵涉哪些部门? | 跨部门信号(杀手锏) | ✓ | 缺省 | 缺省 | agency facet 分布 |
| Q3 | 过去一年哪个社区的排水投诉增长最快? | 时间趋势 | ✓ | 按社区 | ✓ 过去一年 | trend |
| Q4 | 全市 catch basin 堵塞 top10 社区? | 空间排名 | ✓ | 全市,按社区 | 缺省 | topN |
| Q5 | catch basin 堵塞通常还伴随哪些问题? | 共现/关联 | ✓ | 缺省 | 缺省 | 共现 |

覆盖检查:topic 压 5 次;geo 压 3 次(Q1/Q3/Q4);time 压 1 次(Q3);aggregation 覆盖分布/钻取/趋势/topN/共现全部形态;agency facet 于 Q2 主打。schema 每一维均有问题驱动,无空想维度。
- **边界**:不做多轮对话式澄清;不做拼写纠错以外的 query 改写;解析失败时降级为纯 hybrid 检索(不带聚合),不报错中断。
- **验收标准**:(1) 问题集中的 5 个问题全部正确解析为结构化对象;(2) 20 个变体问法(同义改写)解析正确率 ≥ 80%;(3) 单次解析延迟 ≤ 500ms。

### C6 · Hybrid 检索与聚合

- **输入**:C5 输出的结构化 query 对象。
- **输出**:ES 检索结果(BM25 + kNN,RRF 融合,结构化条件下推为 pre-filter)+ FastAPI 层聚合(按社区分组、时间趋势、排名、共现),以 JSON 返回。
- **边界**:排序不引入 learning-to-rank 等训练型 reranker;个性化不做;聚合类型限于问题集覆盖的形态(分组计数、趋势、topN、共现),开放式分析交给使用者。
- **验收标准**:(1) 类"healthy foods"型查询(如"雨水排水问题",字面不匹配任何类目)能命中 Sewer/Catch Basin/Street Flooding 类记录;(2) 精确词查询(如"catch basin clogged")BM25 路召回正常;(3) 端到端 p95 延迟 ≤ 2 秒(本地单机)。

### C7 · 评估 Harness

- **输入**:golden set(约 100 个测试 query,人工标注相关性);FAISS IndexFlatIP 对同一批向量的暴力精确检索结果。
- **输出**:评估报告:recall@10(ES 近似 vs FAISS 精确)、precision(基于 golden set 人工标注)、若采用"query→类目映射"框架则补充 macro-F1。
- **边界**:FAISS 仅在评估中使用,永不出现在在线 serving 路径;评估不覆盖 UI 可用性;不做 A/B 测试(无真实流量)。
- **验收标准**:(1) recall@10 ≥ 0.95(相对 FAISS 精确 ground truth);(2) golden set 上 precision@10 ≥ 0.8;(3) 评估脚本单命令可复现,结果写入版本化报告。

### C8 · Demo 前端

- **输入**:C6 的 JSON 输出。
- **输出**:单页 demo:查询框 + 结果列表 + 至少一种聚合可视化(社区分布图或趋势线)。
- **边界**:不做用户系统、权限、多租户;不追求视觉设计完成度(功能演示优先);长期在线托管为可选项(小 VPS $5–15/月),默认交付形态为录屏 + 截图 + `docker compose up` 本地一键复现。
- **验收标准**:(1) 5 个 chief-DS 问题在 demo 页上全部可演示;(2) 全新机器 clone repo 后 `docker compose up` 一条命令起全套并可用;(3) README 含双叙事线(招聘方视角的工程决策 + 政府视角的信号发现)。

---

## 4. 全局边界(Non-goals,实线)

1. **不上云**:原型全部本地 Docker Compose;云迁移路径(Cloud SQL + Cloud Run / ES 托管)仅写入 roadmap 一页。
2. **不训练任何模型**:无微调、无 learning-to-rank、无自训 embedding。
3. **不做实时流式**:每日 batch 增量即可。
4. **不做 21M 全量**:阶段一 766K 切片 → 阶段二 2025 全年 3.4M;全量仅论证可行性(约 13GB 向量,低于单索引 50–100M 向量舒适上限)。
5. **不做双检索引擎并行 serving**:FAISS 不进在线路径;pgvector 不启用(Postgres 只做事实源)。
6. **不处理非公开数据**:通话录音、个人身份信息一律不碰。

## 5. 全局验收标准(项目级 Definition of Done)

- [ ] `docker compose up` 单命令起全套(ES + Postgres + FastAPI + demo 页)。
- [ ] 阶段一(766K catch-basin 切片)全链路跑通:拉取 → 增强 → 向量化 → 索引 → 检索 → 评估。
- [ ] recall@10 ≥ 0.95(vs FAISS 精确),precision@10 ≥ 0.8(vs golden set)。
- [ ] 5 个 chief-DS 问题全部可在 demo 上演示,其中至少 1 个展示跨部门信号(单条投诉映射到 ≥ 2 部门)。
- [ ] 一次性成本 ≤ $20(LLM enrichment);零持续成本(不含可选 VPS)。
- [ ] README 完成双叙事线;评估报告可复现。
- [ ] 阶段二(2025 全年 3.4M)完成入库与索引,作为"系统可 scale"章节。

## 6. 里程碑(双轨:W0 MVP 冲刺 + v1.1 补齐)

### 轨道 A · W0 一周 MVP(降档验收标准,标注为 v0)

前提:用 Fable 5 Cowork/Code 压缩工程时间;判断任务由 Archy 亲手做;墙钟任务(拉取/编码/enrichment)挂后台并行。数据集仍为 766K catch-basin 切片(成本瓶颈不在行数)。

| 天 | Archy(判断,不可外包) | Cowork/Code(工程) | 后台(墙钟) |
|---|---|---|---|
| D1 | **定 5 个问题集** + facet 维度草案(硬阻塞) | compose 骨架 + 拉取脚本 | Socrata 开拉 |
| D2 | 审 enrichment prompt,抽检首批映射 | embedding 管道 + enrichment batch 脚本 | enrichment 跑 |
| D3 | 复核映射(抽 50 个,v0 降档) | ES mapping + 索引 + FastAPI 骨架 | 编码 766K 向量 |
| D4 | 标 golden set(30 条,v0 降档) | query understanding + hybrid 检索 | — |
| D5 | 读核心代码(RRF / HNSW / 融合逻辑) | 评估脚本 + FAISS 对照 | 评估跑 |
| D6 | 验收 5 个问题的 demo 效果 | demo 页 + 可视化 | — |
| D7 | README 双叙事定稿 | **buffer**(集成 debug 必然吃掉) | — |

v0 降档验收(区别于 §5 全标准):golden set 30 条(非 100)、映射抽检 50 个(非 100)、recall/precision 目标暂不设硬门槛(先跑通并记录基线)。

### 轨道 B · v1.1 补齐(W0 之后,按需推进)

- 补齐 §5 全部验收数字(recall@10 ≥ 0.95、precision@10 ≥ 0.8、映射准确率 ≥ 90%)。
- golden set 扩到 100 条;映射抽检扩到 100 个。
- 阶段二(2025 全年 3.4M)入库与索引。
- README 工程叙事深化(为面试追问准备:RRF 的 k、HNSW 的 ef_search 等)。

## 7. 未决事项(阻塞项优先)

1. ~~**[阻塞 C5] chief-DS 问题集**~~ → **已解决(D1)**:5 个问题已锁定(见 C5),四维确认够用,新增两条约束:`time_range` 可缺省、`aggregation` 需支持 drill-down。
2. **项目名**:Rechannel 为暂定候选,待确认。
3. resolution_description 是否纳入阶段一文本拼接(当前:不纳入,列为扩展)。
4. demo 托管形态:录屏交付(默认)vs 常驻 VPS(可选)。
