# 字幕节奏与镜头边界校正台

本地运行的单页 Web 应用：导入 **SRT / WebVTT 字幕** 与 **含镜头起止毫秒的 JSON**，
按 Unicode 字素簇（grapheme cluster）计数，检查：

- 相邻字幕**重叠**与**最小间隔**不足；
- **每秒字符数（CPS）**超限；
- 字幕**跨越镜头切换点**（可设置边界容差，起止都贴近边界时仅提示）。

设置「字符速率上限 / 最小间隔 / 边界容差」后，系统**只平移起止点、保持原时长不变**，
搜索满足全部约束的**确定性校正方案**，按以下顺序逐级择优：

1. 总移动毫秒 `Σ|Δ|` 最少；
2. 改动条数（Δ≠0）最少；
3. 改动的 `cue_id` 集合按字典序最小（cue_id 越小越优先保持不动）。

无法满足时，输出最小化定位的**冲突链**（固定障碍 + 连续可移动段的最早/最晚排程交叉点）。
CPS 只取决于时长，平移无法修复，这类条目标记为 `cps_conflict` 并单列冲突链。

支持逐条 / 批量采纳，导出含**原文 SHA256、校正版本号、逐条校正与冲突说明**的 JSON。

## 技术栈

- 后端：Python 3.11 + FastAPI + SQLite（`regex` 按 UAX #29 切字素簇）
- 前端：原生 JavaScript / HTML / CSS，无构建步骤
- 持久化：SQLite（`data/app.db`，Docker 下挂载到宿主机 `./data`）

## 方式一：Docker Compose 一键启动（推荐）

```bash
docker compose up --build
```

启动后浏览器访问：

> http://localhost:8000

数据保存在宿主机 `./data/app.db`，停止后数据不丢失；如需重置：

```bash
docker compose down
rm -f data/app.db
```

## 方式二：本地直接运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

访问 http://localhost:8000 。数据库路径可用环境变量 `APP_DB_PATH` 覆盖，
默认 `./data/app.db`。

## 使用步骤

1. **导入数据**：选择 `examples/sample.srt` 与 `examples/shots.json`（可一起导入），点击「导入选中文件」。
   - 字幕：`.srt` / `.vtt`，自动识别；
   - 镜头 JSON：`[{"start":0,"end":2400}, ...]` 或 `{"shots":[...]}`，
     键名兼容 `start_ms/startMs/in/from`、`end_ms/endMs/out/to`，时间为毫秒（数字）或时间码字符串。
2. **设置参数**：CPS 上限（字素簇/秒，默认 20）、最小间隔（ms，默认 40）、边界容差（ms，默认 120），保存。
3. **检查问题**：查看重叠、间隔、CPS、跨镜头问题（error / info 两级）。
4. **搜索校正方案**：仅平移、保时长的确定性最优方案；查看 L1 总移动、改动条数与逐条 Δ。
5. **采纳**：逐条勾选，或「全部采纳可采纳项」/「清除采纳」。冲突条不可采纳。
6. **导出 JSON**：含原文 SHA256、校正版本号（每次导出自增）、逐条校正（仅采纳项写入 correction）、冲突链与说明。

## 导出 JSON 结构

```jsonc
{
  "app": "subtitle-rhythm-shot-boundary-correction-desk",
  "exported_at": "…(UTC ISO8601)",
  "correction_version": 1,
  "source": {
    "subtitle": { "filename": "sample.srt", "format": "srt",
                  "sha256": "…", "cue_count": 5 },
    "shots":    { "filename": "shots.json", "shot_count": 5 }
  },
  "settings": { "max_cps": 20, "min_gap_ms": 40, "boundary_tolerance_ms": 120 },
  "summary":  { "cue_count": 5, "moved_count": 2, "total_abs_shift_ms": 130,
                "conflict_count": 0, "cps_conflict_count": 1,
                "adopted_count": 2, "feasible": false },
  "conflict_chains": [
    { "type": "cps_intrinsic|timing_chain", "reason": "…",
      "pivot": "cue-0004", "cue_ids": ["…"], "roles": { "…": "member|fixed_left|fixed_right" } }
  ],
  "cues": [
    {
      "cue_id": "1", "order": 0, "text": "…", "grapheme_count": 12,
      "original":  { "start_ms": 1200, "end_ms": 3500 },
      "duration_ms": 2300,
      "adopted": true,
      "correction": { "start_ms": 1160, "end_ms": 3460, "delta_ms": -40 },
      "proposed_correction": { "…" },
      "status": "corrected|unmodified|conflict|cps_conflict",
      "original_issues": ["…"],
      "remaining_issues_after_proposal": [],
      "conflict_note": null
    }
  ]
}
```

## 约束与择优说明

- 平移变量为每条 cue 的起点（毫秒整数），结束点同步平移，时长 `d_i` 恒定；
- 相邻约束 `z_i ≥ z_{i-1} + d_{i-1} + min_gap`；
- 镜头安全：存在镜头 `[S,E]` 使 `S-tol ≤ z_i` 且 `z_i+d_i ≤ E+tol`（多镜头区间取并集）；
- CPS 超限的 cue 固定不动作为障碍参与排程，并输出 `cps_intrinsic` 冲突链；
- 不可行段以前向最早 / 反向最晚排程定位交叉点，输出含左右障碍的 `timing_chain`。

## 目录结构

```
app/
  main.py        FastAPI 路由（导入/设置/分析/求解/采纳/导出）
  solver.py      确定性平移求解器（盒约束 L1 保序回归 + 分层 DP + 冲突链）
  analyzer.py    重叠/间隔/CPS/跨镜头检测
  parsers.py     SRT / WebVTT / 镜头 JSON 解析
  grapheme.py    Unicode 字素簇计数（regex \X，UAX #29）
  db.py          SQLite 持久化
  static/        前端（index.html / styles.css / app.js）
examples/        示例字幕与镜头
```
