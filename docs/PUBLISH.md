# 上传 GitHub 并上架 MaiBot 插件市场

> 目标：让别人在 MaiBot WebUI 的插件市场里搜到「视频识眼」，点一下就能装。
> 关键只有一句话：**插件是独立公开仓库；Tag、Release、`_manifest.json` 的 `version` 三者必须完全一致。**

---

## 0. 上架前必须满足的硬条件

| 条件 | 说明 |
|---|---|
| 公开 GitHub 仓库 | 插件中心通过 `_manifest.json` 的 `urls.repository` 定位仓库 |
| 根目录四个文件 | `_manifest.json`、`plugin.py`（含 `create_plugin()`）、`LICENSE`、`README.md` |
| 至少一个 Release | **草稿 Release 或只打 Tag 都不算**，必须是已发布的 Release |
| 版本三段式 | `version`、`host_application.*`、`sdk.*` 都必须是 `X.Y.Z`，不能带 `-rc1` / `+build` |
| ID 稳定 | 发布后改 `id` 会被判为「发布版本改变了插件 ID」而驳回 |

---

## 1. 先把 manifest 改成你自己的信息

打开 `_manifest.json`，把这三处换成你的账号：

```json
"id": "com.saltin.video-vision",          // 建议改成 github.<你的用户名>.video-vision
"author": { "name": "Saltin", "url": "https://github.com/SaltinA" },
"urls": {
  "repository": "https://github.com/SaltinA/maibot-video-vision",   // 不带 .git
  "homepage": "...",
  "documentation": ".../blob/main/README.md",
  "issues": ".../issues"
}
```

> `id` 的格式是 `^[a-z0-9]+(?:[.-][a-z0-9]+)+$`：全小写，至少一个 `.` 或 `-`。
> **一旦提交过插件中心就不要再改**，改了所有历史版本都会失效。

同时改 `plugin.py` 里的常量：

```python
PLUGIN_ID = "com.saltin.video-vision"   # 与 manifest 的 id 保持一致
```

---

## 2. 上传到 GitHub

### 方式 A：网页（最省事）

1. GitHub → New repository → 名称 `maibot-video-vision`，**Public**，不要勾选初始化 README；
2. 进入空仓库 → 「uploading an existing file」→ 把 `maibot01/` 里的**内容**（不是目录本身）拖进去；
3. 提交。

> ⚠️ `_manifest.json` 以 `_` 开头，Windows 资源管理器默认不显示这类文件时容易漏传。上传后务必刷新仓库首页确认它在根目录。

### 方式 B：命令行

```bash
cd maibot01

# 1) 初始化
git init -b main
git add .
git commit -m "feat: 视频识眼 v1.0.0，让 MaiBot 看见视频"

# 2) 关联远端（改成你的仓库地址）
git remote add origin https://github.com/<your-name>/maibot-video-vision.git
git push -u origin main
```

`.gitignore` 已经排除 `__pycache__/`、`config.toml`、测试临时目录 —— 用户配置和字节码不该进仓库。

---

## 3. 打 Tag + 发 Release（决定市场能不能装）

```bash
# Tag 与 manifest 的 version 对齐：v1.0.0 或 1.0.0 都行，manifest 里必须是 1.0.0
git tag v1.0.0
git push origin v1.0.0

# 用 gh 创建 Release（也可在网页 Releases → Draft a new release 里点）
gh release create v1.0.0 --title "v1.0.0 · 首个版本" --notes "见 CHANGELOG.md"
```

三个必须对齐的点：

| 位置 | 值 | 说明 |
|---|---|---|
| Git Tag | `v1.0.0` 或 `1.0.0` | 允许 `v` 前缀，其余部分必须是纯三段式 |
| `_manifest.json` → `version` | `1.0.0` | 不能有后缀 |
| Release | 基于同一个 Tag | 必须是「已发布」，不是草稿 |

另外：**该 Tag 指向的 commit 里必须能读到 `_manifest.json`**（改写历史删过它的 Tag 会被驳回）。

---

## 4. 提交到插件中心（Issue 方式，推荐）

1. 打开 <https://github.com/Mai-with-u/plugin-repo/issues/new/choose>
2. 选择 **「Add Plugin / 添加插件」**
3. 填写：
   - **插件 ID**：与 `_manifest.json` 的 `id` 一致
   - **仓库地址**：`https://github.com/<you>/maibot-video-vision`（公开 HTTPS，无 `.git`）
   - 勾选四项 checklist
4. 提交后 CI 会自动拉取你仓库根目录的 `_manifest.json` 做校验，结果以评论形式回在 Issue 里。

标签流转：

```
pending-validation → validated → approved        （成功）
pending-validation → validation-failed           （失败）
```

**校验失败怎么办**：按评论里的错误改仓库 → 在 Issue 里评论 `/recheck` → CI 重新校验。
只改代码不留言，Issue 不会自己恢复。

审核通过后，维护者用 `/approve` 批准，插件进入插件中心索引。索引同步是**定时任务**，不是即时生效；刚通过时搜不到属于正常现象，等一轮同步。

---

## 5. 怎么让别人搜得到

插件市场的搜索数据来自插件索引（`plugins.json` / `plugin_versions.json`），字段来源就是你的 manifest：

| 会被索引/展示 | 来自 manifest |
|---|---|
| 插件名 | `name` |
| 简介 | `description` |
| 分类筛选 | `plugin_type` |
| 作者、仓库、文档链接 | `author` / `urls` |

所以「能被搜到」的实操建议：

1. **`name` 里放用户真会搜的词**：本插件叫「视频识眼」，但如果用户搜「视频」只靠名字匹配可能失手——把 **`description` 写成包含关键词的自然语言**，例如：
   > 「让麦麦真正看见视频：抽帧 + 视觉理解……支持视频消息识别、视频内容理解、video / vlm」
2. **`plugin_type` 选对**：本插件是 `media`，会落在「媒体处理」分类里。
3. **不要试图堆关键词字段**：manifest v2 是 Pydantic 严格模式，`categories`、`keywords`、`repository_url` 这些**未定义字段会导致校验失败**，加了反而上不了架。
4. 通过后在插件详情页点安装，用户看到的就是你 Release 里的版本；README 会被渲染，写清楚配置和依赖能显著减少 issue。

---

## 6. 发新版本的正确姿势

```bash
# 1) 改代码
# 2) 改 _manifest.json 的 version → 1.0.1
git add -A
git commit -m "fix: 修复 xxx"
git push

# 3) 同名 Tag + Release
git tag v1.0.1
git push origin v1.0.1
gh release create v1.0.1 --title "v1.0.1" --notes "..."
```

规则：

- **同一版本首次收录后，Tag 和 commit 必须保持不变**；要改代码就发新版本号。
- 删除已收录的 Release 会被标记为「撤回」，并保留历史记录。
- 预发布版本用 GitHub 的 **Prerelease** 标记，不要往 `version` 里写 `-beta`。

---

## 7. 提交前自检清单

```bash
# manifest 能被解析（多余的逗号/注释会在这里报错）
python -m json.tool _manifest.json

# Tag 与 manifest 版本是否一致
git describe --tags --abbrev=0
python -c "import json; print(json.load(open('_manifest.json'))['version'])"

# 插件本体自检（不连 QQ、不花钱）
python tests/test_offline.py
```

- [ ] 仓库是 Public
- [ ] 根目录有 `_manifest.json`（`manifest_version: 2`）、`plugin.py`、`LICENSE`、`README.md`
- [ ] `id` 稳定唯一、`version` 三段式
- [ ] `host_application` / `sdk` 上界写 `999.999.999`，只认真约束 `min_version`
- [ ] `author` 是 `{ name, url }` 对象，`url` 是 http(s)
- [ ] `urls.repository` 是公开 HTTPS 且不带 `.git`
- [ ] `capabilities` 只声明真正用到的（本插件：`api.call`、`llm.generate`、`message.get_recent`）
- [ ] 至少一个已发布 Release，Tag 与 manifest 版本一致
- [ ] 在真实 MaiBot 里重载过插件，视频能被识别（别只跑本地测试）

---

## 8. 常见驳回原因对照

| Issue 里的提示 | 原因 | 修法 |
|---|---|---|
| 读不到 `_manifest.json` | 仓库非公开 / 文件不在根目录 / 链接带了 `.git` | 检查仓库可见性与路径 |
| `extra fields not permitted` | manifest 里有未定义字段（如 `categories`） | 删掉 |
| 版本不兼容 | `host_application` / `sdk` 区间把上界锁死在小版本 | 上界改 `999.999.999` |
| 有 N 个发布版本未通过校验 | Tag 与 manifest 版本不一致 / 该 Tag 里没有 manifest / 改了 `id` | 重新打一致的 Tag 并新建 Release |
| `LICENSE` 相关失败 | 根目录没有 LICENSE，或类型与 `license` 字段不一致 | 放 MIT LICENSE，manifest 写 `"MIT"` |
| 市场能搜到但装上加载失败 | 没在真实 MaiBot 验证 / 上界锁死 / 依赖冲突 | 本地重载复验；上界放开 |
