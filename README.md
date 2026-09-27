# 港口泊位与航道调度

纯Python标准库实现的港口泊位与航道调度原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、靠泊可行性、吃水安全、时间窗冲突和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8321
```

默认端口为`8321`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `POST /api/records/{id}/assess-berthing`：靠泊潮位校核，请求体`{"actual_draft_m":11.2}`，返回ETA前后两小时最低潮位、安全余量、短缺米数和最早可靠泊时刻。
- `GET/POST /api/tides`：潮位看板列表（可按`berth`筛选）与按泊位录入潮时/潮高；同一泊位同一潮时重复录入返回409并提示已有记录。
- `POST /api/tides/{id}/revise`：修正潮高（`tide_height_m`+`reason`），修改前后潮高写入`tide_revisions`只追加历史。
- `GET /api/tide-revisions`：修正历史，可按`tide_id`或`berth`筛选。

靠泊（`berth`动作）会强制进行潮位校核：取预计到达时刻前后两小时窗口内
的最低潮位（相邻潮时线性插值），实际水深=泊位基准水深+最低潮高，安全余量=
实际水深-实际吃水；余量不足0.5米时拒绝靠泊，错误响应`details.berthing_assessment`
中给出`shortage_m`（还差多少米）与`earliest_berth_hour`（最早可靠泊时刻）。
校核通过时，完整校核结果作为快照写入记录`payload.berthing_assessment`与审计事件，
后续补录或修正潮位不影响已完成的历史航次。潮位录入/修正角色为
`port_controller`、`duty_officer`（`admin`同权）。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
