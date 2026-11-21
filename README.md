# 多维分析查询与指标平台

面向数据分析师和业务用户，提供多维聚合查询、指标口径、分群圈选和数据集市的数据分析平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/routers/versioning.py 语义映射版本化接口（修订版次/落图/血缘回查）
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/semantic_revision.py 映射修订生命周期服务
│   ├── app/mapping_store.py   映射修订版次仓库（版次/血缘边/报表引用/落图批次）
│   ├── app/services/base.py     共享服务骨架
│   ├── app/store.py          内存数据仓库与示例数据
│   └── scripts/scenario_e2e.py 版本化场景端到端验证
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

版本化端到端场景验证（需先起后端）：

```bash
python3 -I backend/scripts/scenario_e2e.py
```

覆盖双版次并存查询、旧报表按原引用回查、销售额替换列、落图中断重试、暂停新批次、
未确认边阻断发布、确认后发布切换等 42 项断言。

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 指标定义 | `metric_def` | 指标 | 指标编码、指标名称、业务域 |
| 多维立方 | `cube_builder` | OLAP立方 | 立方名称、事实表、维度列表 |
| 切片查询 | `slice_query` | 查询模板 | 模板名称、数据立方、查询维值 |
| 分群圈选 | `segment_rule` | 分群规则 | 分群名称、人群类型、筛选条件 |
| 看板布局 | `dashboard_layout` | 看板配置 | 看板名称、所属业务域、图表布局 |
| 即席导出 | `adhoc_export` | 导出任务 | 任务名称、查询SQL、导出格式 |
| 报表订阅 | `report_subscription` | 订阅配置 | 订阅名称、报表对象、推送周期 |
| 异常检测 | `anomaly_detect` | 检测规则 | 规则名称、监控指标、检测算法 |
| 下钻路径 | `drill_path` | 下钻配置 | 路径名称、起始维度、下钻层级 |
| 语义层 | `semantic_layer` | 语义定义 | 语义名称、物理表、字段映射 |
| 查询加速 | `query_accelerator` | 加速配置 | 配置名称、目标查询、加速方式 |
| 归因模型 | `attribution_model` | 归因配置 | 模型名称、归因对象、归因窗口 |
| 同期群分析 | `cohort_analysis` | 群组配置 | 群组名称、初始事件、回访事件 |
| 数据血缘 | `data_lineage` | 血缘节点 | 节点名称、节点类型、上游列表 |
| 查询调度 | `query_scheduler` | 调度计划 | 计划名称、查询语句、执行周期 |
| 结果缓存 | `result_cache` | 缓存条目 | 缓存键、查询描述、结果行数 |
| 数据范围 | `olap_scope` | 范围规则 | 规则名称、数据范围、授权对象 |
| 实验分析 | `ab_analysis` | 实验配置 | 实验名称、对照组、实验组 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 语义字段映射版本化

语义字段修改不再原地覆盖，而是生成不可变的映射修订版次（`mapping_store.py`），
血缘接口按版次读取，报表按固化的版次引用回查。

- **修订版次**：草稿 → 待确认 → 已确认 → 已发布；发布后旧版次转「已替代」但保留可回查。
- **字段血缘边**：每条边有 confirmed/unconfirmed 状态，未确认边不进入血缘图，
  确认版次之前新字段不会写入已发布关系。
- **落图批次**：暂停开关生效时新批次挂起；中断批次可幂等重试，只登记已确认边。
- **报表引用**：`report_mapping_refs` 固化报表生成时的版次，旧报表刷新后仍按原引用
  回查旧字段链，不随后续发布漂移。

主要接口（前缀 `/api/semantic-revisions`）：

| 接口 | 说明 |
| --- | --- |
| `GET /` | 修订版次列表（可按 semantic_id 过滤） |
| `POST /replace-column` | 为销售额替换一列，生成下一版次（新边默认未确认） |
| `POST /landing/pause`、`/resume`、`GET /landing/status` | 落图暂停/恢复/台账 |
| `POST /{revision_id}/landing`、`POST /landing/{batch_id}/retry` | 落图批次与中断重试 |
| `POST /edges/{edge_id}/confirm`、`POST /{revision_id}/publish` | 边确认与版次发布 |
| `GET /lineage/by-version/{semantic_id}` | 按版次读取血缘（留空读当前已发布） |
| `GET /lineage/report/{report_name}` | 旧报表字段链按原引用回查 |
| `POST /reports/bind`、`GET /reports/refs` | 报表版次绑定与引用清单 |

前端在导航「语义版本管理」（`/semantic-versioning`）提供对应工作台。

领域：数据工程
子方向：数据分析与数据应用
