# 从零到上架：MaiBot 插件发布分步教程

> 这份文档是**照着敲就能走完**的版本：每一步都有「目的 / 命令 / 预期输出 / 自检点 / 出错怎么办」。
> 全程 7 步，预计 20 分钟（不含等审核）。

---

## 全程总览

| 步骤 | 做什么 | 完成标志 |
|---|---|---|
| 1 | 改 manifest 为你自己的信息 | `_manifest.json` 里没有占位符 |
| 2 | 本地自检 | `python tests/test_offline.py` 输出 `✅ 全部通过` |
| 3 | 建公开仓库并推送 | 网页上能看到 `_manifest.json`、`plugin.py`、`LICENSE`、`README.md` |
| 4 | 打 Tag + 发 Release | Releases 页面有一个**已发布**的 `v1.0.0` |
| 5 | 提 Issue 申请上架 | Issue 标签从 `pending-validation` 变成 `validated` |
| 6 | 等人工批准 | 标签变成 `approved` |
| 7 | 安装并验收 | 市场里搜得到、装上能用 |

> 你现在（2026-10-23）已经走完 1~5，卡在 6。可以对照最后一节「本次实录」复盘。

---

## 步骤 1：把 manifest 改成你自己的信息

**目的**：插件中心靠这个文件识别你是谁、仓库在哪、支持哪个版本的麦麦。

打开 `_manifest.json`，逐项对照：

| 字段 | 必须写成 | 例子（本项目实际值） |
|---|---|---|
| `id` | `github.<你的用户名>.video-vision`，全小写 | `github.saltinanon.video-vision` |
| `author.name` | 你的昵称 | `Saltin` |
| `author.url` | 你的主页，**必须 http(s)** | `https://github.com/SaltinAnon` |
| `urls.repository` | 仓库地址，**不带 `.git`** | `https://github.com/SaltinAnon/maibot-video-vision` |
| `urls.homepage` / `documentation` / `issues` | 同上，指向同一账号 | 见文件 |
| `version` | `1.0.0` 这种三段式 | `1.0.0` |

同时把 `plugin.py` 里的常量改成一致的 id：

```python
PLUGIN_ID = "github.saltinanon.video-vision"
```

⚠️ **三个坑，踩了就很难看**

1. **`id` 一旦提交过插件中心，终身不能改**。改了等于发布了一个新插件，旧版本全部作废。
2. **四个 URL 必须指向同一个真实存在的账号**。这个项目就栽过一次：`author.url` 改成了 `SaltinAnon`，但 `urls.repository` 还留着 `SaltinA`（少了 `non`），结果市场里的“仓库”按钮会 404，版本索引也可能找不到 Release。
3. **不要添加 `categories`、`keywords` 之类的字段**。manifest 是 Pydantic 严格模式，多一个未定义字段就直接校验失败。

### 自检点

```bash
python -m json.tool _manifest.json > /dev/null && echo "JSON OK"
grep -o 'https://github.com/[^"]*' _manifest.json
```

预期：`JSON OK`，并且输出的四行**前缀完全一致**。

---

## 步骤 2：本地自检（别跳过）

**目的**：装机之前先证明「代码能跑、组件能被 SDK 收集、缓存逻辑成立」。

```bash
python tests/test_offline.py
```

预期输出：

```
[1] _manifest.json
  PASS  必填字段齐全
  ...
[4] 端到端流程
  PASS  重复视频没有再调视觉模型
  PASS  缓存摘要同样注入
✅ 全部通过
```

> 这个脚本不需要 QQ、不需要 API Key：它会用 Pillow + ffmpeg 现场合成一段视频，把整条链路跑一遍。

### 出错怎么办

| 报错 | 原因 |
|---|---|
| `缺少 ffmpeg` | 装 `pip install imageio-ffmpeg`，或配置 `runtime.ffmpeg_path` |
| `ModuleNotFoundError: maibot_sdk` | `pip install maibot-plugin-sdk` |

---

## 步骤 3：建公开仓库并推送

**目的**：插件中心只认**独立的公开 GitHub 仓库**，不接受子目录、压缩包、私有库。

### 3.1 网页建库

1. GitHub → **New repository**
2. 名称 `maibot-video-vision`，可见性 **Public**
3. **不要**勾选 “Add a README file”（否则推送会冲突）

### 3.2 本地初始化并推送

```bash
cd maibot01

git init -b main
git add .
git commit -m "feat: 视频识眼 v1.0.0，让 MaiBot 看见视频"

git remote add origin https://github.com/SaltinAnon/maibot-video-vision.git
git push -u origin main
```

### 3.3 自检点

打开仓库首页，**根目录**必须同时看到这四个：

```
_manifest.json    plugin.py    LICENSE    README.md
```

⚠️ `_manifest.json` 以下划线开头，Windows 资源管理器默认可能不显示它，上传/提交时容易漏。提交后用 `git show --stat HEAD` 确认它在列表里。

### 出错怎么办

| 报错 | 原因与修法 |
|---|---|
| `schannel: server closed abruptly` | 本机代理/证书问题。换后端重试：`git -c http.sslBackend=openssl push` |
| `Authentication failed` | 用 PAT（Personal Access Token）当密码，或先 `gh auth login` |
| `failed to push some refs` | 远端已有 README 等文件。`git pull --rebase origin main` 后再推 |

---

## 步骤 4：打 Tag + 发 Release（最容易翻车的一步）

**目的**：插件市场按 **Release** 安装，不按分支。所以「Tag」和「Release」缺一不可。

### 4.1 为什么必须这样做

插件中心的同步工具会扫描你仓库的 Git Release，为每个 Tag 生成一条版本记录。**草稿 Release 不算、只打 Tag 不发 Release 也不算。** 三者版本号必须完全一致：

| 位置 | 值 | 说明 |
|---|---|---|
| Git Tag | `v1.0.0` 或 `1.0.0` | 允许 `v` 前缀 |
| `_manifest.json` → `version` | `1.0.0` | 纯三段式，不能带 `-rc1` / `+build` |
| Release | 基于同一个 Tag，**已发布** | 不是 Draft |

### 4.2 命令

```bash
# Tag 打在「manifest 版本正确」的那个提交上
git tag v1.0.0
git push origin v1.0.0

# 创建 Release（gh 需要先 gh auth login；也可以网页操作）
gh release create v1.0.0 --title "v1.0.0 · 首个版本" --notes "见 CHANGELOG.md"
```

网页操作路径：仓库右侧 **Releases → Draft a new release → 选择 `v1.0.0` → 填标题 → Publish release**。

### 4.3 自检点

```bash
git describe --tags --abbrev=0
python -c "import json; print(json.load(open('_manifest.json'))['version'])"
```

两行输出都应该是 `v1.0.0` / `1.0.0`（去掉 `v` 后一致）。

然后打开 `https://github.com/<你>/<仓库>/releases`，确认看到 **Latest** 标记，而不是草稿。

---

## 步骤 5：提 Issue 申请上架

**目的**：把仓库登记进插件中心索引。

1. 打开 <https://github.com/Mai-with-u/plugin-repo/issues/new/choose>
2. 选择 **「Add Plugin / 添加插件」**
3. 逐字段填写：

| 表单字段 | 填什么 |
|---|---|
| 插件 ID / Plugin ID | 与 `_manifest.json` 的 `id` **完全一致** |
| 仓库地址 / Repository URL | `https://github.com/<你>/<仓库>`（公开 HTTPS，不带 `.git`） |
| 4 个复选框 | 全部勾选 |
| 补充说明 | 可选，一句话介绍即可 |

4. 提交后不要关页面，等 1~2 分钟。

### 标签会怎么变

```
你提交 → pending-validation（机器排队）
       → validated（CI 通过）
       → approved（维护者批准，已入库）
```

失败则变成 `validation-failed`。

### 自检点

Issue 里出现 `github-actions` 的 **✅ 验证通过 / Validation Passed** 评论，且标签是 `validated`。

### 出错怎么办

按评论里的提示改仓库 → 在 Issue 里评论 **`/recheck`** → CI 重新跑。
**只改代码不留言，Issue 不会自己恢复。**

---

## 步骤 6：等人工批准

**目的**：机器只检查格式，人检查“这东西该不该进市场”。

- 标签变成 `approved` 就说明入库了；
- 之后还有一轮**定时索引同步**（天级别），所以刚 approved 时搜不到属于正常；
- 不建议反复催，一次礼貌的补充说明足够。

### 这个阶段还能改代码吗？

**能，但要看有没有被收录。** 这是最容易踩的分界线：

| 状态 | 能不能改已发布版本的内容 |
|---|---|
| 还没 `approved`（未收录） | ✅ 可以：改完把 Tag 移到新提交（见下） |
| 已经 `approved`（已收录） | ❌ 不行：Tag 和 commit 必须保持原样，改代码只能发新版本号 |

### 修正已发布版本的 Tag（仅在未收录时）

场景：你推了 `v1.0.0`，但发现 manifest 写错了（比如仓库链接写错账号）。

```bash
# 1) 改文件 → 提交 → 推 main
git add -A
git commit -m "fix: 修正 manifest 链接"
git push

# 2) 把 Tag 移到这个新提交（版本号不变）
git tag -f v1.0.0
git push -f origin v1.0.0
```

> Tag 只是指针，Release 跟着**标签名**走，所以 Release 会自动指向新提交，内容不用重建。
> 移完建议在 Issue 里评论 `/recheck`，让 CI 针对新 commit 再校验一次。

**如果已经被收录了**，就只能走升级流程（见附录 A）。

---

## 步骤 7：安装并验收

**目的**：证明“能搜到”和“能跑”是两件事。

### 7.1 安装

1. MaiBot WebUI → 插件管理 → 插件市场
2. 搜「视频识眼」或「Video Vision」
3. 安装 → 在插件列表里启用 → 重载

### 7.2 配置（装完必须检查）

| 配置项 | 值 | 说明 |
|---|---|---|
| `vision.task_name` | `vlm` | 麦麦的「看图」任务 |
| 模型管理里的该模型 | `visual = true` | 不支持视觉的模型会报 `vision_failed` |
| `detect.trigger_mode` | `mention_or_private` | 私聊或被 @ 才解析（省 token） |

### 7.3 三条验收（缺一不可）

1. **私聊发一段视频** → 消息里出现 `[视频内容] …`（≈1.7k token 的一次性成本）
2. **`/video status`** → 能看到 ffmpeg 路径、今日计数、缓存命中数
3. **同一个视频再发一次** → 计数不涨、摘要后出现「缓存」→ 证明 0 token 复用生效

---

## 附录 A：发新版本（升级流程）

```bash
# 1) 改代码
# 2) 改 _manifest.json 的 version -> 1.0.1
git add -A
git commit -m "fix: 修复 xxx"
git push

# 3) 新 Tag + 新 Release（版本号必须和 manifest 一致）
git tag v1.0.1
git push origin v1.0.1
gh release create v1.0.1 --title "v1.0.1" --notes "..."
```

规则：

- 同一版本**首次收录后**，Tag 和 commit 不可变；要改就发新版本。
- 删除已收录的 Release 会被标记为「撤回」并保留历史。
- 预发布版本用 GitHub 的 **Prerelease**，不要往 `version` 里写 `-beta`。

---

## 附录 B：命令速查

```bash
# 全部检查一次
python -m json.tool _manifest.json > /dev/null && echo "JSON OK"
python tests/test_offline.py
git describe --tags --abbrev=0
python -c "import json; print(json.load(open('_manifest.json'))['version'])"

# 首次发布
git tag v1.0.0 && git push origin v1.0.0
gh release create v1.0.0 --title "v1.0.0" --notes "首个版本"

# 未收录时修 Tag
git tag -f v1.0.0 && git push -f origin v1.0.0
```

---

## 附录 C：常见驳回对照表

| Issue 提示 | 真实原因 | 修法 |
|---|---|---|
| 读不到 `_manifest.json` | 仓库私有 / 文件不在根目录 / 链接带 `.git` | 检查可见性与路径 |
| `extra fields not permitted` | manifest 里有未定义字段（`categories`、`keywords`） | 删掉 |
| 版本不兼容 | `host_application` / `sdk` 上界锁死在小版本 | 上界改 `999.999.999` |
| 有 N 个发布版本未通过校验 | Tag 与 manifest 版本不一致 / 该 Tag 读不到 manifest / 改过 `id` | 重新打一致的 Tag 并新建 Release |
| LICENSE 相关失败 | 根目录没 LICENSE，或与 `license` 字段不一致 | 放 MIT LICENSE，manifest 写 `"MIT"` |
| 市场能搜到但装不上 | 没在真实 MaiBot 验证 / 上界锁死 / 依赖冲突 | 本地重载复验 |

---

## 附录 D：术语

| 词 | 含义 |
|---|---|
| Tag | Git 的版本指针，`v1.0.0` → 某个 commit |
| Release | GitHub 基于 Tag 的发布记录，市场按它安装 |
| 索引 | 插件中心的 `plugins.json` / `plugin_versions.json` |
| 收录 | 版本被写入索引，此后该 Tag/commit 不可变 |
| `validated` | CI 通过（机器检查），**不等于**上架 |
| `approved` | 维护者批准，进入索引同步队列 |

---

## 本次实录（2026-10-23）

| 项 | 实际情况 |
|---|---|
| 仓库 | `SaltinAnon/maibot-video-vision`（Public，main） |
| 插件 ID | `github.saltinanon.video-vision` |
| Tag / Release | `v1.0.0` → commit `b2d481e`（修正链接后移动） |
| Issue | plugin-repo **#944**，CI 通过，`validated`，等 `/approve` |
| 踩过的坑 | ① 配置模型缺 `plugin.config_version` → 加载即报错 ② 4 个 URL 误写 `SaltinA` → 市场链接 404 ③ 纯 dHash 把不同纯色场景判成同帧 → 叠加颜色网格 |
