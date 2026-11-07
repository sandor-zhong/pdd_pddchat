# 图形化发布工具

在开发电脑上双击项目根目录的 `launch_publisher.cmd`，即可打开发布工具。该工具不会放进客户安装包。

## 已配置的服务器

- SSH/SFTP：`root@47.107.143.148:22`
- 服务器更新根目录：`/var/www/pdd-appeal-updates`
- 本地SSH密钥：`C:\Users\Administrator\.ssh\aliyun_license_admin`
- 公开更新地址：`https://flowtry.top/updates/pdd_appeal/`

工具通过SSH的SFTP子系统传文件，不需要在服务器安装Python或新增API。密钥路径和连接资料保存在 `packaging/publisher-state/profile.json`，不保存密钥内容或密码。该目录已忽略提交，也不会进入发布包。

## 日常发版

工具现在提供两种“发布模式”：

- **完整发布**：生成完整安装包和一份通用代码包。适用于首次上线、依赖变化和更新器升级。
- **日常通用代码发布**：跳过Inno Setup安装包编译，只生成和上传一份通用代码ZIP、签名及新版清单，不包含EXE，也不需要选择旧版本基线。

**首次启用需完整发布支持通用包的0.1.7版本，让客户端升级更新器。** 完整发布同时更新 `latest.json`、`latest-v2.json` 和 `latest-v3.json`；日常通用代码发布只更新 `latest-v3.json`，前两个入口继续指向最近一次完整发布，兼容旧客户端。

本次已生成可上传的完整发布目录：`dist/updates/20261105010752/`，安装包为 `dist/pddchatSetup-0.1.7-lite.exe`。重新打开工具，点击“上传已有发布包”选择这个目录，发布后让客户端先升级到0.1.7。之后日常选择“日常通用代码发布”，无需选择基线。`dist/updates/20261105010948/` 为0.1.8的本地验证包，未上传；它用于验证不生成EXE和跨旧构建更新。

同一份代码包可以给不同旧构建使用，不逐版本制作差异包。客户端会校验运行环境指纹、更新器、公钥和数据版本，兼容才应用；不兼容时提示获取完整安装包。日常通用模式不自动下载EXE兜底。

通用包包含最新的全部受管理代码，客户端根据自身旧清单清理已废弃代码；不删除未纳入清单的本地文件。不打包Python环境、浏览器、`.env`、登录态、订单数据、截图和GUI本地配置。替换前先备份，等待新版GUI启动确认后提交，失败回滚。

1. 完成代码修改和测试，双击打开发布工具。
2. 填写新版本号和更新说明。每行一条说明。
3. 日常修改选择“日常通用代码发布”；首次启用或环境、更新器变化时选择“完整发布”。界面已移除基线选择。
4. 点击“测试连接”。已有可信known_hosts记录的主机可直接验证。新主机会显示SHA256主机指纹，核对服务器指纹后填入。指纹不一致时工具会拒绝连接。
5. 选择发布模式，点击“一键构建并上传”。工具先检查连接和目录，随后按所选模式完成构建、签名、上传及HTTPS验证。
6. 等待显示“发布成功，HTTPS验证通过”，再让客户端在“帮助 → 检查更新”中升级。

发布的版本号会写入 `release_config.json`。完整发布的安装包在 `dist/`，服务器文件在 `dist/updates/<构建编号>/`，同时生成 `dist/pddchat-update-server-<构建编号>.zip` 供手动部署。日常通用模式只包含 `incremental/pddchat-code-<构建号>.zip`、独立签名及 `latest-v3.json` 和签名，不生成EXE或旧版清单。

## 只打包或重试上传

- “仅生成发布包”：构建并签名，生成上传压缩包，不连接服务器。
- “上传已有发布包”：选择包含更新清单的构建目录。工具优先读取 `latest-v3.json`，并兼容历史的v2和旧清单。上传失败后可直接重试，无需重新打包。工具从签名清单识别发布模式，与下拉框当前选择无关。已上传且SHA256一致的文件会跳过；同名但内容不同的文件拒绝覆盖。
- “打开发布目录”：打开最近生成或选择的发布文件目录。

工具不会删除服务器旧包。先上传所选模式的包和独立签名，通过SSH在服务器运行 `sha256sum` 校验文件后，最后原子替换对应清单，再访问HTTPS验证。进度按字节数计算，并显示大小、平均速度和校验阶段，不会把大文件下载回来校验。服务器需提供 `sha256sum` 并支持OpenSSH的 `posix-rename` 扩展。

如果提示“服务器已发布，但HTTPS验证未通过”，表示清单已经切换，只是外部地址验证失败。先检查HTTPS站点目录映射、证书及缓存；随后可使用同一个发布目录重试，不要为此重复构建新版本。

## 日志和锁

日志：`packaging/publisher-state/publisher.log`。每次成功上传会记录 `last-published.json`。工具自动检查既有更新器与环境兼容性，不再要求手选基线。密码不保存，也不会传入构建命令参数。

本机使用 `packaging/publisher-state/build.lock` 防止两个工具同时构建；服务器使用更新目录下的 `.publish-lock` 防止两个工具同时发布。正常完成或报错时自动释放。电脑或网络异常中断后如遗留锁，先确认没有其他构建/上传任务，再手动清理对应锁；服务器只需删除这个空锁目录，不要删除更新根目录。

## 开发环境依赖

当前开发机已安装发布工具依赖。换电脑后可使用：

```powershell
uv --cache-dir .\packaging\downloads\uv-cache pip install `
  --python .\gui\.venv\Scripts\python.exe `
  -r .\packaging\publisher-requirements.txt
```

需保留既有打包环境、Inno Setup、两份暂存Python运行时，以及发布签名私钥 `packaging/signing-private/release-key.xml`。SSH登录密钥与发布签名私钥是两种不同的密钥，均不要上传更新服务器。

实现参考：[Paramiko SSH客户端](https://docs.paramiko.org/en/stable/api/client.html)、[SFTP原子重命名](https://docs.paramiko.org/en/stable/api/sftp.html#paramiko.sftp_client.SFTPClient.posix_rename)。
