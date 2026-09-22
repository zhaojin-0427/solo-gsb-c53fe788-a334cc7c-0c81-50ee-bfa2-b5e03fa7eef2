# 字幕节奏与镜头边界校正台（Subtitle Rhythm & Shot-Boundary Correction Station）

本地运行的 Web 应用：导入 **SRT / WebVTT** 字幕与含镜头起止毫秒的 **JSON**，
按 **Unicode 扩展字素簇（UAX #29）** 计数，检查重叠、空隙、每秒字符数（CPS）
与跨镜头切换；在「只平移、不改时长」的前提下搜索确定性最优校正方案，
支持逐条采纳并导出带**原文哈希、校正版本、冲突说明**的 JSON。

## 技术栈

- 后端：Python 3.11 + FastAPI + SQLite（`fastapi` / `uvicorn` / `regex`）
- 前端：原生 HTML + CSS + JavaScript（无构建、无框架）
- 部署：Docker Compose 一键启动，数据持久化在命名卷中

## 一键启动（Docker Compose）

```bash
docker compose up -d --build
```

启动后浏览器访问：

> **http://localhost:8000/**

停止：

```bash
docker compose down          # 保留数据卷
docker compose down -v       # 同时删除已导入的项目数据
```

SQLite 数据库位于容器内 `/data/subtitle_station.db`（Compose 命名卷
`subtitle-data`）。如需改用宿主机目录，可把 `docker-compose.yml` 的卷改成：

```yaml
volumes:
  - ./data:/data
```

## 本地直接运行（不使用 Docker）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 数据库路径（可选，默认 ./subtitle_station.db 由 DATABASE_PATH 控制）
export DATABASE_PATH="$(pwd)/subtitle_station.db"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

访问 http://localhost:8000/ ，接口文档在 http://localhost:8000/docs 。

## 使用流程

1. **导入素材**
   - 字幕：`.srt` 或 `.vtt`（自动识别；计数时去除标签、折叠空白，
     按扩展字素簇计数，空格计入字符，CJK 与 emoji 组合序列按 1 个可见字符计）。
   - 镜头 JSON（可选）：
     ```json
     [
       {"start_ms": 0,    "end_ms": 1200},
       {"start_ms": 1200, "end_ms": 3050}
     ]
     ```
     也接受 `{"shots": [...]}` 外层，字段名兼容 `start/end`、`in/out`、
     `start_ms/end_ms` 等毫秒字段。
2. **设置参数**
   - **CPS 上限**：每秒字符数告警阈值（平移不改变时长，CPS 仅作检测告警，
     无法通过平移修复时会在条目上标注）。
   - **最小间隔（ms）**：相邻条目之间允许的最小空隙；重叠（负值）与过小空隙
     都会被检出。
   - **镜头边界容差（ms）**：条目整体必须落在某个镜头内；起点可在
     `原起点 ± 容差` 内平移，且平移后条目不跨过镜头切点。
3. **查看结果**
   - 统计条：间隔/重叠、CPS、跨镜头、可提案、需移动、冲突数量与总移动量。
   - 表格逐行显示原文哈希、原时间、字素数、CPS、问题标记、提案时间与 Δ。
   - 无法满足约束的 cue 以**冲突链**形式列出（含导致失败的相邻关系说明）。
4. **逐条采纳**：每行「采纳 / 撤销」，或顶部「采纳全部提案」；采纳状态持久化。
5. **导出**
   - `JSON`：含项目参数、每条原文 SHA-256、`original` / `proposal` /
     `corrected` 三套时间、状态以及完整冲突说明，并带整包 `document_hash`。
   - `SRT`：导出采纳后的时间轴（未采纳者回退提案、再回退原值）。
   - `ZIP`：同时包含上述 JSON 与 SRT。

## 求解器说明（确定性最优）

对每一个与冲突链不相交的**连续可解后缀段**独立求解，搜索空间为各条目在
「镜头窗口 ∩ 原起点 ± 容差」内的所有整数毫秒起点，约束为：

- `t[i-1] + duration[i-1] + min_gap ≤ t[i]`
- 条目整体位于某镜头窗口内
- 仅平移，`duration` 保持原值

采用整数区间上的**分段线性动态规划（后缀极小包络）**精确求解，择优按
字典序严格排列：

1. **总移动毫秒** `Σ|t_i − original_i|` 最小；
2. 并列时，**发生移动的条目数**最少；
3. 再并列时，按 **`cue_id` 字典序**排列的绝对偏移向量字典序最小
   （越靠前的 cue_id 越优先保持原位；正负向偏移相同时取较小起点）。

权值采用超递增整数系数（Python 大整数精确运算，无浮点比较），
因此相同输入永远产生唯一、确定的输出。不存在可行解的条目归入**冲突链**：
链是连续的不可解段，说明中给出硬阻断的相邻 cue 对或“无镜头窗口可容纳”等
内在冲突原因。

### 运行测试

```bash
python3 tests/test_bruteforce.py   # 5000 个随机小规模用例：DP 解 vs 全枚举最优
python3 tests/test_perf.py         # 500 条目真实尺度性能/约束校验
```

## 目录结构

```
app/
  main.py        FastAPI 路由（导入/分析/设置/采纳/导出）与静态文件托管
  database.py    SQLite 持久化（projects / cues / shots / adoptions）
  subtitles.py   SRT/WebVTT 解析、字素簇计数、原文 SHA-256
  shots.py       镜头 JSON 解析
  analysis.py    检测编排（重叠/空隙/CPS/跨镜头）与统计
  corrector.py   分段线性 DP 求解器与冲突链构造
  static/        原生前端（index.html / style.css / app.js）
tests/           全枚举等价性测试与性能冒烟测试
docker-compose.yml / Dockerfile
```
