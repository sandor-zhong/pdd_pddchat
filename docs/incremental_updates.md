# HTTPS 增量更新与发布

适用于 Windows 安装版。服务器只需要提供 HTTPS 静态文件，不需要数据库或更新 API。

固定更新根地址：`https://flowtry.top/updates/pdd_appeal/`，定义在根目录 `update_core.py`。

## 启动确认与故障诊断（2026-11-05）

新版更新器最多等待180秒，必须收到本次更新的启动确认才提交；进程退出、确认超时均会走原有回滚流程。提示中分别显示退出码或超时原因。更新尝试目录中的 `gui-startup.log` 保存窗口日志启用前的标准输出/错误，`module-check.log` 保存模块导入失败信息；`app/gui/state/logs/gui.log` 的“启动阶段”包含进程号和阶段累计耗时。

GUI在窗口正常显示并运行事件循环后发送确认，再在后台采集首次内存快照。通用更新后的下一次更新优先使用发布清单中哈希匹配的 `app/update_agent.py`、`app/update_core.py`，否则使用稳定的 `updater` 目录。

已经复制并运行中的旧更新器不会被这次代码修改替换，仍适用旧的40秒期限。首次迁移建议使用包含修复的完整安装包；尚未重新打包、安装时，运行中的客户端行为保持原样。

## 当前推荐：通用代码包

图形化发布工具已改用一份通用代码更新包，移除了基线选择。完整发布生成EXE与全部代码ZIP；日常发布只生成代码ZIP、签名和 `latest-v3.json`，不生成EXE。客户端按自身旧清单清理废弃代码，并检查运行时、更新器及数据版本兼容性，不依赖某一个旧构建号。详细使用方式以 [发布工具说明](publisher_tool.md) 为准。

首次需完整安装支持 `universal_code` 的0.1.7版本。新客户端优先读v3清单，404时依次兼容v2和旧清单。日常通用发布只更新v3；完整发布更新三个入口。以下按基线增量方式和v2清单说明保留供旧版及命令行兼容参考，不再是图形工具的日常流程。

## 完整发布与日常仅增量发布

推荐使用根目录 `launch_publisher.cmd`，在“发布模式”中选择。完整发布保留EXE兜底；日常增量发布跳过安装包编译，只生成并上传变化的代码、签名和 `latest-v2.json`。

首次需要完整发布0.1.5或更新版本，让客户端具备仅增量能力。旧基线清单必须包含 `update_features: ["incremental_only"]`，并且更新器、公钥及运行时保持兼容；否则工具要求改为完整发布。

新版客户端先读取 `latest-v2.json`，该文件不存在（HTTP 404）时兼容读取 `latest.json`。完整发布同时更新两个入口，仅增量发布只更新新版入口，旧入口继续指向最近一次完整发布。旧客户端不会读到缺少EXE的清单。

仅增量版本的清单包含 `publish_mode: "incremental"`、`full: null` 和至少一个增量包。无法匹配基线的新版客户端提示获取完整安装包，不进行下载或替换。

命令行仅增量构建与发布示例（旧构建必须已支持此功能）：

```powershell
.\packaging\build_installer.ps1 -Version 0.1.6 -CodeOnly
.\.venv\Scripts\python.exe packaging\release_tools.py publish `
  --mode incremental `
  --base .\packaging\releases\旧构建编号\app_release.json `
  --notes-file .\packaging\release_notes.txt
```

这个发布目录只有 `latest-v2.json`、对应签名、独立签名和增量ZIP，不含EXE或 `latest.json`；可以直接映射上传。以下完整安装包示例继续适用于完整发布（命令行默认 `--mode full`）。

## 首次部署

旧安装版没有更新器，需要先覆盖安装一次新版。源码运行环境不能原位更新。

本次已在开发机生成一套 RSA 3072 位发布密钥：

- `update_public_key.json`：公钥，随程序分发。
- `packaging/signing-private/release-key.xml`：**发布私钥，必须单独离线备份，不能上传服务器、发给用户或提交 Git。** 该目录已经加入 `.gitignore`。

不要删除或重新生成私钥；既有客户端只信任最初安装时分发的公钥。私钥丢失后需通过可信渠道重新安装软件建立新的信任。

全新开发环境首次创建密钥时才运行以下命令；已有密钥会拒绝覆盖：

```powershell
cd D:\PythonProjects\pddchat
.\.venv\Scripts\python.exe packaging\release_tools.py init-key
```

构建支持更新的完整安装包：

```powershell
.\packaging\build_installer.ps1
```

当前版本来自 `release_config.json`。每次构建自动生成不同的 UTC 时间构建编号，写入 `app_release.json`。

安装包输出到 `dist\pddchatSetup-<版本>-lite.exe`。本轮构建快照和安装包回执保存在 `packaging\releases\<构建编号>\`。**保留这些快照**，以后生成增量包需要用到。

生成首次服务器发布文件：

```powershell
.\.venv\Scripts\python.exe packaging\release_tools.py publish --notes-file .\packaging\release_notes.txt
```

脚本读取本轮安装包回执，验证安装包和暂存源码属于同一构建，自动计算大小、SHA256 并签名。首次发布没有旧基线，只有完整安装包，这是正常情况。

输出到 `dist\updates\<新构建编号>\`：

```text
latest.json
latest.json.sig
packages/
    pddchatSetup-<版本>-<新构建>.exe
    release-<新构建>.json.sig
incremental/
```

**只上传该发布目录的内容**，不要上传整个项目、`packaging` 或密钥目录。

## 以后发布增量版本

1. 修改程序，完成测试。
2. 修改 `release_config.json` 中的版本，例如 `0.1.2`。
3. 构建安装包，记录控制台输出的新构建编号。
4. 选择已经分发给用户的旧构建快照，生成增量包。

```powershell
cd D:\PythonProjects\pddchat
.\packaging\build_installer.ps1

# 将占位内容替换成真实旧构建编号
.\.venv\Scripts\python.exe packaging\release_tools.py publish `
  --base .\packaging\releases\旧构建编号\app_release.json `
  --notes-file .\packaging\release_notes.txt
```

可选：明确指定本次构建编号和版本。构建编号必须是 3～20 位数字且不能复用，后续版本必须更大：

```powershell
.\packaging\build_installer.ps1 -Version 0.1.2 -Build 202611050001
```

增量 ZIP 包只包含修改/新增的程序文件及内部清单，记录应删除的旧程序文件。旧清单的 SHA256 必须完全匹配，不能手工改快照。

首版每次发布支持指定一个旧基线。不匹配该基线的客户端自动下载完整安装包，不会错误套用补丁。若需要支持多条旧版本直升路径，可在后续扩展发布工具，不能直接手改签名清单。

以下情况自动使用完整安装包：

- 无匹配的旧构建，或本地受管理程序文件被改动。
- 两份 Python 运行时/依赖的构建指纹变化。
- 独立引导更新器、公钥或程序路径大小写发生变化。

## 上传顺序

将生成目录中的内容映射到以下服务器位置：

```text
https://flowtry.top/updates/pdd_appeal/
    latest.json
    latest.json.sig
    packages/<安装包和独立签名文件>
    incremental/<增量ZIP>
```

先上传 `packages/`、`incremental/`，最后用服务器原子替换方式发布 `latest.json`。`latest.json.sig` 也可以保留，供离线核验；客户端实际下载的是 `packages/release-<构建编号>.json.sig`，所以不会混用新旧签名。

包和签名文件名包含构建编号，发布后不可覆盖。旧包、旧签名先保留，避免更新中的客户端访问失败。

服务器建议：

- `latest.json`：`Content-Type: application/json`，`Cache-Control: no-cache, max-age=0`。
- ZIP、EXE、独立签名文件可以长期缓存，但不能重写为网站首页或返回登录页。
- 必须保持 HTTPS 证书验证；客户端不接受 HTTP、跨域重定向或超出指定更新目录的 URL。
- 不需要开启目录索引，能直接访问文件即可。

不需要服务器端执行 Python，也不需要把服务器密码填进客户端。本次实现不自动上传服务器。

## 客户端操作

新版安装后：`帮助 → 检查更新`。启动 5 秒后也会后台检查一次；无更新或检查失败时不会自动弹错误框。发现更新且当前空闲时显示更新窗口，任务运行中则只显示状态栏提示。

用户选择“下载并更新”后：

1. 检查当前没有申诉、复议、查询、批量登录、导出或其它操作窗口。
2. 下载并验证清单签名、包大小及 SHA256。
3. 独立更新进程再次验证，取得与业务任务共用的运行锁。
   它使用缓存目录中的独立标准库运行时，不占用安装目录内的 Python 文件，完整安装包才能替换原运行时。
4. 主程序退出后才修改文件，不会强行终止正在处理的订单。
5. 增量更新先备份再替换，模块检查及新版 GUI 启动确认通过后提交；失败回滚并重新启动旧版。

完整包降级使用 Inno Setup 覆盖安装并重新检查版本；**完整安装包不提供整个 Python 运行时的事务回滚**，失败时应重新运行完整安装包。增量回滚不涉及数据库迁移，首版不允许在更新过程中修改业务数据库结构。

## 数据保护与恢复

增量包仅允许修改顶层 Python 程序文件、公钥/发布元数据及 `gui/app/` 下的指定代码资源。以下内容不在更新范围：

- `.env` 和本地密钥。
- `state/`：订单数据库、浏览器登录态。
- `gui/state/`：账号、调度及 GUI 本地数据。
- `gui/config.json`、凭证、输出文件、日志和浏览器运行时。

完整安装也不打包这些用户数据，并保留已有的 `gui/config.json`。

更新下载、备份、事务日志和启动确认文件保存在**安装目录下的 `update-cache/`**，与 `app/` 同级。不要在更新过程中删除它。更新失败可查看对应任务目录中的 `failed.json`、`result.json`、`transaction/journal.json`，完整包还会产生 `installer.log`。

桌面快捷方式通过稳定启动入口 `updater/update_launcher.py` 打开。更新进程异常退出后，再次打开会在加载 GUI 前检查并恢复未提交的增量事务；如果新版进程仍存活，会先要求关闭它，避免边运行边回滚。不要绕开快捷方式直接启动 `app.main` 作为日常启动方式。

备份不会自动清理。确认更新稳定、没有更新进程运行后，可以手工清理较早任务的缓存；至少保留最近一次的备份与日志供排查。

## 清单格式

`latest.json` 由工具生成，主要字段如下：

```json
{
  "schema_version": 1,
  "app_id": "pdd_appeal",
  "channel": "stable",
  "version": "0.1.2",
  "build": "202611050001",
  "release_notes": ["本次更新说明"],
  "release": {
    "schema_version": 1,
    "app_id": "pdd_appeal",
    "version": "0.1.2",
    "build": "202611050001",
    "runtime_id": "自动计算的运行时指纹",
    "files": {"main.py": "自动计算的文件SHA256"}
  },
  "full": {"url": "完整包HTTPS地址", "sha256": "自动计算", "size": 123456},
  "incremental": [{
    "from_build": "旧构建编号",
    "base_manifest_sha256": "旧app_release.json的SHA256",
    "url": "增量ZIP的HTTPS地址",
    "sha256": "自动计算",
    "size": 12345
  }]
}
```

示例中的哈希和地址是占位说明，不能直接放上服务器；修改已签名 JSON 会导致签名验证失败。

## 实现依据

签名验证使用 Windows 的 [BCryptVerifySignature](https://learn.microsoft.com/en-us/windows/win32/api/bcrypt/nf-bcrypt-bcryptverifysignature)；解包逐项检查路径及哈希，不对下载 ZIP 直接调用 `extractall`，参见 [Python zipfile 文档](https://docs.python.org/3/library/zipfile.html)。
