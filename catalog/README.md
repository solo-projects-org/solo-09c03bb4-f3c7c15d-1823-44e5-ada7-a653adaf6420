# Satellite Scene Catalog API

目录服务：遥感数据员登记卫星景的 **B04（红光）/ B08（近红外）** 两个波段文件，
农情分析组按 **地块经纬矩形 + UTC 时段** 查询现行景，并下载已验真的波段文件。

技术栈：FastAPI + SQLite（WAL）+ 本地内容寻址文件存储，Docker Compose 一键启动，
自带 Swagger 入口。

## 1. 启动

```bash
cd catalog
docker compose up --build -d
```

- Swagger UI：<http://localhost:8000/docs>
- OpenAPI JSON：<http://localhost:8000/openapi.json>
- 健康检查：<http://localhost:8000/health>
- 数据持久化在命名卷 `catalog-data`（SQLite 库 + 波段文件，容器重启不丢）

## 2. 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/scenes/{scene_id}/revisions` | multipart 表单提交一个修订（元数据 + B04/B08 两文件 + 各自 SHA-256） |
| GET | `/scenes` | 按 bbox（`west,south,east,north`）与 `start_time/end_time`（UTC ISO-8601）查询，只返回每个景的**现行修订** |
| GET | `/scenes/{scene_id}/revisions/{revision}` | 取任意历史修订（含被取代的旧修订）及其下载地址 |
| GET | `/scenes/{scene_id}/revisions/{revision}/bands/{band}/download` | 下载 B04/B08，响应头带 `X-SHA-256` |
| GET | `/health` | 存活/数据库检查 |

### 提交示例

```bash
curl -X POST http://localhost:8000/scenes/S2A_MSIL1C_001/revisions \
  -F "revision=1" \
  -F "acquisition_time=2026-06-01T10:00:00Z" \
  -F "west=116.0" -F "south=39.0" -F "east=117.0" -F "north=40.0" \
  -F "sha256_b04=$(sha256sum b04.tif | cut -d' ' -f1)" \
  -F "sha256_b08=$(sha256sum b08.tif | cut -d' ' -f1)" \
  -F "file_b04=@b04.tif" \
  -F "file_b08=@b08.tif"
```

### 查询示例

```bash
curl "http://localhost:8000/scenes?west=116.2&south=39.2&east=116.8&north=39.8&start_time=2026-06-01T00:00:00Z&end_time=2026-06-30T23:59:59Z"
```

## 3. 一致性规则（如何满足需求）

1. **双波段验真才可查/可下**：上传先流式落临时盘并实时计算 SHA-256，
   与申报值逐字节比对；任一不符返回 `400` 且不写任何目录记录。只有两份文件
   都通过后才在同一事务里写入 scene + 两条 band 记录。
2. **同修订重复提交幂等**：`(scene_id, revision)` 为主键。内容完全一致的重复提交
   返回 `200` + `submission_status=idempotent_replay`，响应与首次完全相同；
   内容（元数据或任一摘要）冲突返回 `409`，原记录不受影响。
3. **迟到旧修订不能盖新版**：提交时校验 `revision > 该景当前最大修订号`，
   否则 `409 stale revision`。修订号允许跳号。
4. **新版改变时空范围后查询只返回相交的现行景，且不随历史修订增长而变慢**：
   除保存全部修订的 `scenes` 外，另有一张 **现行景加速表 `current_scenes`（每景仅一行）**，
   在每次提交的同一事务内增量维护，并对获取时间建索引；当 SQLite 构建支持 R*Tree 时，
   再由 `current_scenes_rtree` 虚拟表把矩形查询变成空间树检索（不支持时自动降级为普通表，
   依然与历史修订数解耦）。`GET /scenes` 只读加速表，复杂度为 O(现行景数)，
   **窄窗口不再扫描任何历史修订**；矩形相交在 R*Tree 候选上再用 64 位 REAL 精确复算，
   语义与原查询逐字节一致。新修订迁移覆盖范围/时间后，加速表在同事务内整体替换该景的一行，
   旧位置不会回流、新位置提交即可见；WAL 下并发查询只能看到提交前或提交后的完整状态，
   不会出现同一景新旧拼接。其他景的提交只改动自己那一行，无需全库重建。
5. **旧修订仍按原地址下载**：历史 `scenes`/`bands` 行与内容寻址文件全部保留、不可变、
   永不删除，`/scenes/{id}/revisions/{n}/...` 下载地址长期有效，新修订发布不影响进行中的下载。
6. **已有 SQLite 目录升级后直接可用，且加速数据不漏不错**：启动时按 `PRAGMA user_version`
   判定，旧库（仅 `scenes`/`bands`）在一次事务内从历史全量重建加速表（幂等、可重入，
   中断后下次启动重做）；每次启动还会做一致性校验（行数、现行修订号、元数据、R*Tree
   包围盒逐行比对，兼容其 float32 存储精度），发现漂移立即重建，重建后仍不一致则拒绝启动。
   提交失败/回滚、幂等重传、服务重启都与加速表在同一事务或同一校验流程内，
   不会让加速数据漏项或错项。
7. **校验失败 / 并发 / 重启不会让目录指向缺失文件**：
   - 文件先 `os.replace` 原子落位，数据库行后插入；失败时临时文件立即清理；
   - 写事务使用 `BEGIN IMMEDIATE` + WAL + `busy_timeout`，并发提交被序列化，
     相同内容并发提交只有一个 `201`，其余幂等 `200`，冲突内容一个成功一个 `409`；
   - 启动时执行目录完整性检查（每条 band 记录的文件必须存在），缺失则拒绝启动；
     同时清理崩溃遗留的 `tmp/upload-*.part`。

## 4. 本地测试（无 Docker 时）

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt httpx pytest
pytest            # 25 个端到端用例（含并发、重启、下载交错、现行景加速与迁移）
uvicorn app.main:app --port 8000
```

测试覆盖：双波段验真、校验失败无残留、幂等回放、冲突拒绝、迟到修订拒绝、
时空过滤与现行修订、新旧修订下载交错、4 路并发相同/冲突提交、重启持久化、
缺失文件拒绝启动、临时文件启动清理；`tests/test_current_index.py` 额外覆盖
旧库启动迁移、索引漂移自修复、查询计划不扫历史表且随历史增长保持平坦、
无 R*Tree 构建降级、失败/幂等不污染加速表、查询与提交并发不出现新旧拼接、
以及其他景提交只改自身索引行。
