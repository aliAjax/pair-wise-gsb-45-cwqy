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
- `GET /api/tides`：潮位看板列表，可带`berth`参数。
- `POST /api/tides`：录入潮位，请求体为`{"berth":"B12","tide_hour":8,"height_m":1.2}`；同一泊位同一潮时重复录入返回409提示已有数据。
- `POST /api/tides/{id}/corrections`：修正潮位，请求体为`{"height_m":1.6,"reason":"..."}`，修改前后值均留痕。
- `GET /api/tides/{id}/corrections`：某条潮位的修正历史。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 潮位看板与靠泊审批

- 值班员（角色`duty_officer`）按泊位录入潮时（0-23整点）与潮高，修正同样由值班员提交并保留修改前后记录。
- 靠泊审批（`berth`动作）取预计到达前后两小时内的最低潮位，叠加泊位基准水深后与实际吃水比较；安全余量不足0.5米时返回422，报文说明还差多少米并给出最早可靠泊时刻；窗口内无潮位数据时拒绝审批。
- 审批结果快照（最低潮位、可用水深、安全余量等）写入航次`payload.tide_check`及审计事件，历史航次查询不受后续潮位补录或修正影响。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
