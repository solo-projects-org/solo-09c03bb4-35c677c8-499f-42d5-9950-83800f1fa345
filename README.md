# 备件组 · 月报抽查工单核对

备件组抽查维修站月报上的换件次数：月报里每个组合申报了 N 次更换，每次更换
对应一张工单（凭证）。抽查时按组合抽中若干序号（1..N），中心需要向站点
逐项索取这些序号对应的工单号。本项目用 Flask 实现：

* **两个独立的站点服务**（`station/`，同一镜像各部署一份）：月报与凭证
  CSV 按版配对存放，只暴露两个受控点查接口，中心拿不到全量数据；
* **一个中心抽查网页**（`web/`）：选月份、上传抽查 CSV 建单，逐项列出
  工单号；站点超时的项可在原单上续取，订单落盘、重启后仍可继续。

## 目录结构

```
station/
  app.py                 # 站点服务：/api/declared、/api/voucher 两个点查接口
  Dockerfile
  requirements.txt
  data/
    station_a/<月份>/<版号>.report.csv     # 月报：combo_id,count（申报次数）
    station_a/<月份>/<版号>.vouchers.csv   # 凭证：combo_id,seq,work_order
    station_b/...                          # B 站同理
web/
  app.py                 # 中心网页（Flask 薄层，urllib 调站点接口）
  core.py                # 抽查单核心逻辑：解析、校验、状态机、持久化（纯逻辑）
  templates/index.html   # 上传建单 + 抽查单列表
  templates/order.html   # 订单页：逐项工单号 + 取数/续取
  Dockerfile
  requirements.txt
tests/test_core.py       # 核心逻辑自测（26 个用例，无需网络）
scripts/e2e_check.py     # 端到端自测（自起进程，含超时续取/重启/撤版场景）
docker-compose.yml       # 三个服务一键启动
```

## 启动（Docker Compose）

```bash
docker compose up --build
```

* 中心抽查网页：http://localhost:8000
* 维修站A 服务：http://localhost:8001
* 维修站B 服务：http://localhost:8002

抽查单持久化在命名卷 `web-data` 里，重启 web 容器后仍可在原单上续取。
站点数据目录只读挂载，更换/新增某版数据：在 `station/data/station_x/<月份>/`
下放入 `<版号>.report.csv` 与 `<版号>.vouchers.csv` 配对文件即可（无需重启，
逐请求读取）；**撤版** = 撤下该版的两个配对文件。

## 使用流程

1. 首页选月份，上传抽查 CSV 建单。CSV 表头需含
   `station,version,combo_id,seq`（站点、月报版号、组合编号、凭证序号，
   支持中文表头）；站点列可填 `a`/`b` 或站名（如 维修站A）。
2. **建单前校验**（任一不满足即整单退回，不落单）：
   * 同一站在同一单中必须使用同一版月报；
   * 按表中版号向站点点查申报次数，凭证序号须在 1 至申报次数内；
   * 查不到指定月报（月份或版号不存在）、站点不可达，同样整单退回。
3. 订单页点“取数/续取”，中心只对抽中的明细逐条点查凭证，逐项列出工单号：
   * **已确认**：拿到工单号；
   * **缺凭证**：原版凭证 CSV 无对应记录；
   * **待取回**：站点超时/不可达，站点恢复后再点“取数/续取”即可在原单上续取；
   * **原版已撤下**：站点已查不到该版月报，页面提示**重新发起抽查**。
4. 已确认项保留原结果：续取只碰待取回项，迟到的响应（无论成败）都不会
   覆盖已确认/缺凭证/已撤下的项。订单落盘（`WEB_DATA_DIR/orders.json`，
   原子写），中心服务重启后仍可继续续取。

## 站点接口（中心只能这样取数）

1. `GET /api/declared?month=YYYY-MM&version=版号&combo=编号`
   查该版月报上某组合的申报次数；组合缺号计 0；月份或版号不存在返回 404。
2. `GET /api/voucher?month=YYYY-MM&version=版号&combo=编号&seq=序号`
   查该版第 seq 号凭证的工单号；无对应记录返回 `found=false`（缺凭证）；
   月份或版号不存在（已撤下）返回 404。

中心建单校验用 `/api/declared`，取数用 `/api/voucher`，均为按抽中明细的
点查，不拉取站点全量数据。

## 本地自测

不使用 Docker 时（需本机有 Python 3.11 与 Flask）：

```bash
# 1) 核心逻辑单测（假站点，无需网络）
python3 -I tests/test_core.py

# 2) 端到端自测：脚本自起两个站点 + 中心网页（临时目录、空闲端口），
#    覆盖建单、退回、缺凭证、站点超时后续取、中心重启后续取、撤版提示
python3 -I scripts/e2e_check.py
```

手工起服务（端口 8001/8002/8000）：

```bash
STATION_NAME=维修站A STATION_DATA_DIR=station/data/station_a PORT=8001 \
    python3 station/app.py &
STATION_NAME=维修站B STATION_DATA_DIR=station/data/station_b PORT=8002 \
    python3 station/app.py &
AVAILABLE_MONTHS=2026-01,2026-02,2026-03,2026-04 \
STATION_A_URL=http://127.0.0.1:8001 STATION_B_URL=http://127.0.0.1:8002 \
WEB_DATA_DIR=web/data PORT=8000 python3 web/app.py &
```

## 示例数据说明

内置 4 个月的演示数据，每月至少一版；`2026-01` 两站各有 v1/v2 两版
（v2 为修订版，申报次数与凭证不同）。A 站 `2026-01/v1` 的 A02、
`2026-03/v1` 的 T02 申报了次数但缺第 2 号凭证，用于演示“缺凭证”。

## 说明与限制

* “取数/续取”为同步执行：待取项多且站点不可达时，请求耗时约为
  项数 × 单条超时（`HTTP_TIMEOUT`，默认 5 秒）。
* 同一订单的取数串行（每订单一把锁），不同订单可并行。
* 订单存储为单 JSON 文件，适合内部抽查量级；损坏时自动备份后从空开始。
