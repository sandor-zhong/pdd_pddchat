# 本次更新上线步骤

本次目标版本：`0.1.2`，构建号：`20261104081959`。

## 上传哪些文件

本次服务器发布目录：

`D:\PythonProjects\pddchat\dist\updates\20261104081959\`

也可以使用 `dist\pddchat-update-server-20261104081959.zip`，压缩包内部已经是服务器更新根目录的结构，不需要再套一层构建号目录。

将其内容放到服务器 `https://flowtry.top/updates/pdd_appeal/` 对应的目录：

```text
pdd_appeal/
  latest.json
  latest.json.sig
  packages/
    pddchatSetup-0.1.2-20261104081959.exe
    release-20261104081959.json.sig
  incremental/
    pddchat-20261104064838-to-20261104081959.zip
```

先上传 `packages/` 和 `incremental/`；最后发布 `latest.json`。推荐先上传为临时文件，再在服务器上重命名替换，避免客户端读到未上传完整的清单。不要编辑已经签名的 JSON。

服务器当前检查结果为 `latest.json` 返回 HTTP 404，尚未完成线上部署。本地生成文件不代表已经上传。

## 怎么测试

1. 在安装了上一版的电脑上，停止所有运行任务。打开“帮助 → 检查更新”，确认当前构建号是 `20261104064838`。
2. 上传以上文件后检查更新，应看到 `0.1.2 / 20261104081959`，方式为“增量更新”，大小约 4.4KB。
3. 点击“下载并更新”。程序会关闭、更新、重新打开；再次检查应显示当前已是最新构建。
4. 核对店铺、登录态、订单记录和本地配置。

前面的0.1.1安装包已经包含更新器，这个基线的程序源码与本轮一致，增量包主要更新版本元数据，用于首次验证升级链路。后续修改程序后，增量包会自动包含实际变化的文件。

如果当前构建号不是上述旧构建，可能选择完整包更新，这是正常行为。源码版不支持原位更新。没有“帮助 → 检查更新”的旧客户端，先覆盖安装本次完整安装包：

`D:\PythonProjects\pddchat\dist\pddchatSetup-0.1.2-lite.exe`

已经安装0.1.2的电脑会显示最新版本，不能用它观察从0.1.1升级的过程。

## 下一次发布

先修改 `release_config.json` 中的版本号，完成代码修改和验证，再执行：

```powershell
cd D:\PythonProjects\pddchat
.\packaging\build_installer.ps1
.\.venv\Scripts\python.exe packaging\release_tools.py publish `
  --base .\packaging\releases\20261104081959\app_release.json `
  --notes-file .\packaging\release_notes.txt
```

上传新生成的 `dist\updates\<新构建号>\` 内容，仍然先传包、最后发布清单。保留旧构建快照和服务器旧包。

运行时依赖或引导更新器变化时会自动改用完整包。首版每轮只生成一个旧基线的增量升级路径；其他构建使用完整包。

发布私钥：`packaging\signing-private\release-key.xml`。务必单独备份，不能上传服务器。上传目录中只有公开发布文件。

更多恢复和服务器配置说明见 `docs\incremental_updates.md`。
