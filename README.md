# 试听分流与报名效果分析服务

面向课程运营的 A/B 分流与报名效果分析后端。从**首次分配**开始固定比较对象，在**观察时间相同**的前提下判断新试听安排是否改变整体报名表现；不连接真实投放或支付，行为数据由合成事件流提供，时钟可推进。

- 技术栈：Python 3.12 · FastAPI · PostgreSQL（SQLAlchemy 2 + psycopg 3）
- 接口文档（Swagger UI）：启动后访问 `http://127.0.0.1:8000/docs`（OpenAPI 3.x：`/openapi.json`）
- 全部时间统一 **UTC**；观察窗口为 `[入组时刻, 入组时刻+观察天数)`，**左闭右开**

## 核心规则（与实现的对应）

| 规则 | 实现 |
| --- | --- |
| 首次分配固定比较对象 | `assignments` 每实验每学员仅一行（首次事实），重复请求/换设备返回原组 |
| 稳定学员标识 | `student_key` 显式提供或系统生成；设备绑定解析匿名请求 |
| 按标识哈希分流 | SHA256(`seed:student_key`) → 当前生效比例配置；比例调整只影响后来学员 |
| ITT 全量分析 | 分析含未曝光/曝光失败者，不做任何事后筛选 |
| 统一截止点 + 成熟样本 | 窗口未结束 → `obs_insufficient`；窗口结束但上报水位未覆盖 → `report_incomplete`（数据等待） |
| 有效报名 | 窗口内创建且窗口结束前未被撤销的 ≥1 笔订单；同学员多笔仅计一次 |
| 事件不可变 | 按 `(source, biz_id, seq)` 去重；取消/退款为独立事件；按 `occurred_at` 评估，撤销先到等待原单 |
| 分析快照 | 每次运行冻结截止点、事件序号、水位线、比例配置；晚报只属于新运行；中断可按原快照续算 |
| 证据门槛 | 两组成功/未成功均 ≥10 才给正态近似 95% 区间；跨零只说“未区分方向”，绝不说“等效” |
| 分流卡方检查 | 按各比例配置生效期间分别算期望再合并；p 值超阈值暂停效果判断并列出异常来源 |
| 受众规则不可变 | 规则变化需新开实验；停招只停新分配，既有观察继续 |

## 快速开始（Docker Compose，一键）

```bash
docker compose up --build
# 服务就绪后：
#   Swagger UI  http://127.0.0.1:8000/docs
#   健康检查    curl http://127.0.0.1:8000/health
```

Compose 含 `db`（postgres:18-alpine，库/用户/密码 `ccdev`）与 `app` 两个服务；建表由应用启动时自动完成。

## 本地开发

```bash
# 1) 准备数据库（容器已带 PostgreSQL 18：127.0.0.1:5432，库/用户/密码 ccdev）
# 2) 安装依赖
/opt/python-3.12/bin/python3 -m venv venv
./venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
# 3) 启动（默认连接 postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/ccdev，可用 DATABASE_URL 覆盖）
./venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 演示（合成行为流 + 可推进时钟）

服务运行后执行：

```bash
./venv/bin/python scripts/demo.py        # DEMO_BASE_URL 可覆盖目标地址
```

四个场景（脚本内置 17 项断言，全部通过才退出 0）：

1. **选择偏差** — 800 名学员、新方案真实效果为 0。系统 ITT 结果区间跨 0（“未区分方向”）；对照的错误做法（把看完介绍的人当作新组）算出约 +21pp 的虚假提升。
2. **观察不足 / 上报未齐** — 入组当天、第 4 天、第 8 天（水位未覆盖）、补齐水位后四种状态依次呈现，分清“现在还不能判断”与“没有效果”。
3. **重复入组与重复报名** — 幂等键重放、换设备返回原组、多笔订单仅计一次、恰在结束时刻的订单不计入、窗口内取消不计入、结束后退款仍计入、事件整批重发被去重、资格不足与停招各自返回原因。
4. **晚报** — 运行 A 冻结快照；迟到订单只进入运行 B；A 结果不变且可回查。

## 测试

```bash
./venv/bin/python -m pytest            # 29 个用例；使用独立测试库 ccdev_test（自动创建）
```

测试库地址可用 `TEST_DATABASE_URL` 覆盖；维护连接用 `TEST_MAINTENANCE_DSN` 覆盖。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/POST | `/clock`、`/clock/advance` | 可推进虚拟时钟（只前进） |
| POST/GET | `/experiments`、`/experiments/{id}` | 登记实验：受众规则、固定种子、比例、报名定义、观察天数 |
| POST | `/experiments/{id}/ratio` | 调整比例（仅作用于后来学员） |
| POST | `/experiments/{id}/stop` | 停招（只停新分配） |
| POST | `/experiments/{id}/assignments` | 申请分配（幂等键 + 稳定学员标识 + 设备） |
| POST | `/experiments/{id}/exposures` | 曝光回执（失败仍保留分配事实） |
| POST | `/events` | 合成行为流批量摄取（去重 + 水位线） |
| GET/POST | `/sources`、`/sources/{name}/watermark` | 各来源“已完整上报”时间界限 |
| POST | `/experiments/{id}/analysis` | 以当前时钟为统一截止点运行分析 |
| POST | `/analysis/{run_id}/resume` | 断点续算同一批次（截止点/快照不变） |
| GET | `/analysis/{run_id}`、`/analysis/{run_id}/students` | 结果与学员分段明细（对账） |
| GET | `/experiments/{id}/students/{key}` | 按学员回查资格、分组、成熟条件与报名依据 |

## 测试账号 / 标识

本服务不设登录鉴权（内部运营工具）。可用以下标识验证：

- 演示脚本生成的实验：`demo-selection-*`、`demo-maturity-*`、`demo-dup-*`、`demo-late-*`（`GET /experiments` 可查）
- 演示学员：`stu-dup`（多笔订单）、`stu-boundary`（结束时刻下单）、`stu-cancel`（窗口内取消）、`stu-refund-late`（结束后退款）、`stu-young`（资格不足）
- 手工验证示例见下

手工验证示例（本地启动后）：

```bash
# 建实验（观察 7 天，50/50，受众 age>=18）
curl -X POST http://127.0.0.1:8000/experiments -H 'Content-Type: application/json' -d '{
  "name":"manual-1","seed":"s1","observation_days":7,
  "ratios":{"control":0.5,"treatment":0.5},
  "audience_rule":{"conditions":[{"field":"age","op":"gte","value":18}]}}'
# 申请分配 → 上报订单 → 推进时钟 8 天 → 推进水位线 → 运行分析 → 查看 /analysis/{run_id}
```

## 已执行的验证（本仓库交付前实际运行）

- `./venv/bin/python -m pytest`：**29 passed**（覆盖分配幂等/跨设备/资格快照/停招/比例调整、事件去重/水位线、窗口边界/撤销退款/乱序撤销、观察不足/上报未齐、晚报快照不可变、CI 门槛与跨零解读、卡方暂停与分期间合并、对账、断点续算、学员回查、统计函数已知值）
- `./venv/bin/python scripts/demo.py`：**17/17 项校验通过**（输出见上文四场景）
- `curl /health`、`/openapi.json`、`/docs`：正常
- 说明：本开发容器内无 Docker，Dockerfile/Compose 未实际构建；配置已经静态检查，本地开发与验证均按上文“本地开发”路径完成。
