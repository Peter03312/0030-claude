# 老式纸绝缘市话电缆接续证明服务

这是一个纯后端 Python/FastAPI 服务。它不假设单张接头表中的端子没有重复，也不把“每个图节点度数必须为二”当成规则；服务以整本接续册为输入，建立带物理 A/B 奇偶标签的端口图，并按连通分量给出端到端证明。

服务目标是让接续工程师可以沿物理端口复核：

- 每个局端馈线只到达声明的用户线对；
- 一对芯线的 A/B 两芯在任何接头处都不得拆对或错并；
- 直通或双次 A/B 交换后的累计极性符合预期；
- 只有具名测试桥允许分支；
- 测试桥旁路必须按 A/B 两芯成对终止于声明封帽；
- 任何连通分量不得包含两个不同局端，也不得闭环；
- 结构错误整单返回 `422`；拓扑冲突返回 `200`，但一定是 `status: failed`、`serviceable: false`，并给出可沿端口检查的证据；
- 空接续册（`routes: []`）属于结构错误，不会把“没有任何线路”误判为已证明。

## 模块划分

| 模块 | 责任 |
| --- | --- |
| `app/parser.py` | 严格 YAML 解析、重复键检查、未知字段/未知引用/类型校验 |
| `app/models.py` | 接续册、端口、边、图等领域模型 |
| `app/topology.py` | 构造芯线、接头、测试桥和封帽的物理端口图 |
| `app/proof.py` | 连通分量、闭环、A/B 奇偶、拆对、极性、封帽旁路证明 |
| `app/http_report.py` | FastAPI 路由和 HTTP 报告 |
| `app/verify.py` | 一次性运行 pytest，并探测生产 `/health` 入口 |

## YAML 接续册格式

### 端局/用户预期线对

`office` 和 `subscriber` 只声明两个物理端口，分别是该线对的 A 芯和 B 芯：

```yaml
routes:
  - id: R1
    expected_polarity: normal
    office:
      a: {node: CO, port: OA}
      b: {node: CO, port: OB}
    subscriber:
      a: {node: SUB, port: SA}
      b: {node: SUB, port: SB}
```

`expected_polarity` 只能是：

- `normal`：局端 A 到用户 A、局端 B 到用户 B；
- `reversed`：预期累计为奇数次 A/B 交换，即局端 A 到用户 B、局端 B 到用户 A。

### 电缆段

一条电缆段包含若干成对芯线。每芯都有两个端点，A/B 两芯必须连接相同的两个节点：

```yaml
segments:
  - id: S1
    pairs:
      - pair: P1
        a:
          endpoints:
            - {node: CO, port: OA}
            - {node: J1, port: s1a}
        b:
          endpoints:
            - {node: CO, port: OB}
            - {node: J1, port: s1b}
```

### 普通接头

普通接头只允许两芯连接，接头边按所接电缆芯线的本地物理 A/B 标签自动判断是直通还是交换。每个物理端口在单个接头内最多出现一次：同一端口被重复写进两张 splice、或同时出现在 splice 与具名测试桥中，属于接头表结构错误，整单 `422`（`duplicate_port`），不会被当作闭环拓扑处理：

```yaml
joints:
  - id: J1
    splices:
      - a: {node: J1, port: s1a}
        b: {node: J1, port: s2b}
      - a: {node: J1, port: s1b}
        b: {node: J1, port: s2a}
```

本例两条 splice 均为 `swap`，即一次双芯 A/B 对调。只对调一芯会在端到端分量和线对配对检查中形成失败证据，不会因单个接头表“看起来可连接”而通过。

### 具名测试桥与封帽

测试桥分别为 A、B 建立导电星点，二者绝不允许混到一起。每组至少三个物理端口：主线入口、主线出口和一个旁路入口。

```yaml
joints:
  - id: J1
    splices: []
    test_bridges:
      - id: TB1
        a_ports: [inA, outA, tapA]
        b_ports: [inB, outB, tapB]

caps:
  - id: CAP1
    a: {node: TIP, port: capA}
    b: {node: TIP, port: capB}
```

`A` 旁路和 `B` 旁路必须使用同一条电缆 pair、沿途不得出现奇数次 A/B splice 交换，并且必须分别终止到同一个封帽声明的 A 端与 B 端。若旁路中一次 splice 对调使 A 桥支路落到封帽 B 端、B 桥支路落到封帽 A 端，即使两条支路使用的电缆 pair 相同，也会产生 `test_bridge_bypass_reversed` 故障证据，并且 `sealed: false`、全册不可送话。普通 degree-two splice 上挂出的封帽会失败；未终止于封帽的旁路也会失败。

`open_ends` 只用于显式声明物理开放端，常用于表达“本应封帽却没有封帽”的故障测试：

```yaml
open_ends:
  - id: OPEN1
    node: TIP
    ports: [capA, capB]
```

## API

### 健康检查

```bash
curl http://localhost:8080/health
```

返回：

```json
{"ok": true}
```

### 提交接续册

请求必须使用 `application/yaml`（也兼容 `application/x-yaml`、`text/yaml`）：

```bash
curl -s \
  -X POST http://localhost:8080/prove \
  -H 'Content-Type: application/yaml' \
  --data-binary @manifest.yaml
```

成功协议响应始终是 HTTP `200`：

- 全册无拓扑冲突：`status: "proved"`、`serviceable: true`；
- 存在拓扑冲突：`status: "failed"`、`serviceable: false`，并返回 `faults` 证据。

YAML 语法、schema、未知字段、重复键、未知节点、端口重复声明等结构错误返回 HTTP `422`。`version` 必须是整数字面量 `1`：由于 YAML 会把 `true`、`1.0` 解析成 Python 的 `True`、`1.0`，服务按**精确类型**校验，二者都按 `invalid_version` 拒绝，绝不会被当作版本 1 而给出可送话结论。拓扑冲突不会被伪装成结构错误，也不会出现任何可送话结论。

节点名和端口名允许包含 `/`（例如 `CO/01`、`J/1`）；报告里的物理端口统一以 `节点/端口` 渲染，证明模块按图邻接关系解析端口，不再用字符串切分反推，因此现场编号中的 `/` 不会使合法测试桥接续册被误判。

### 报告内容

`routes[].path_a` 和 `routes[].path_b` 给出逐跳路径：

- `hops`：每一条芯线、普通接头边或测试桥边；
- `cable_pairs`：沿路径使用的电缆段和 pair，A/B 两路径必须一致；
- `joint_stages`：每个接头的物理入端口、出端口和 `straight`/`swap`；
- `swap_count` 与 `cumulative_polarity`：累计 A/B 交换次数和极性；
- `test_bridge_bypasses`：每个合法测试桥旁路的 A/B 路径和封帽 ID。

`faults` 按端口、故障码、线对、分量等稳定排序，包含端口和完整证据路径。每个故障都属于全册连通分量证明的一部分；局部接头通过不会替代端到端证明。

## 本地开发

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.http_report:app --host 0.0.0.0 --port 8000
pytest
```

## Docker Compose

构建并运行 API：

```bash
docker compose up --build api
```

宿主机端口由 `API_PORT` 控制，默认 `8080`，容器内固定监听 8000：

```bash
API_PORT=9090 docker compose up --build api
```

一次性验证服务会等待 API 健康，运行完整 pytest，再探测生产入口：

```bash
docker compose up --build verify
```

`verify` 退出码为零才表示测试和生产入口探测均成功。

## 测试覆盖

`tests/test_proof.py` 共 19 个用例，覆盖：

1. 合法直通；
2. 双次 A/B 交换；
3. 跨多个接头把两路局端馈线并入同一导电分量；
4. 单次反极；
5. 合规具名测试桥和成封帽旁路；
6. 未封帽旁路；
7. 闭环；
8. 严格 schema 的 422 与拓扑冲突 200/failed 的区别；
9. 空接续册不得被判为已证明；
10. 同一接头端口跨 splice 与测试桥重复时整单 422；
11. 封帽旁路 A/B 交叉反极时给出 `test_bridge_bypass_reversed` 证据；
12. 多类畸形 YAML 一律结构化 422，不泄漏 500；
13. `version: true`、`1.0`、`"1"` 等按精确类型拒绝（`invalid_version`）；
14. 节点名包含 `/` 时，合法测试桥接续册仍能生成线路证明。

测试中的 YAML 是显式构造的测试夹具；应用代码不包含伪造拓扑或硬编码样例结果。
