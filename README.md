# 即梦算力共享平台 (Dreamina Compute Sharing Platform)

本项目是一个基于即梦（Dreamina）CLI 构建的算力共享与任务调度平台。它允许多个 Customer 共享一个或多个 Provider（底层为真实的即梦 VIP 账号）的算力，具备完善的积分计费、并发控制与多层任务队列调度系统。

## 🌟 核心功能

- **并发与排队控制**：支持实时并发限制，当可用 Provider 被占满时，任务自动挂起进入等待队列，绝不掉单。
- **动态优先级调度**：支持 VIP 通道（无限并发）、普通通道（SD2）以及经济排队通道（SD2_Lite）。取消首位排队任务后自动提升后续任务优先级。
- **原子化积分流转**：精确的按时长扣费，任务超时、失败或取消自动退还积分，并记录详细账单流水。
- **状态持久化**：依托 SQLite 保证服务器随时可安全重启，重启后内存状态机将自动重建，恢复所有正在提交或排队中的任务。

## 🛠️ 环境要求

- **Python**: 3.8+
- **即梦 CLI**: 本地必须安装 `dreamina` 工具，并在配置中指定执行路径。

## 🚀 安装与启动

1. **安装依赖**：
   ```bash
   pip install -r requirements.txt
   ```
   *(注：请确保包含 Flask 等基础库)*

2. **配置系统**：
   修改 `config.yaml` 文件中的以下项：
   - `dreamina.stub_mode`: 生产环境请改为 `false`。
   - `dreamina.cli_command`: 改为您的即梦 CLI 的绝对路径，例如 `E:/path/to/dreamina.exe`。

3. **启动服务**：
   ```bash
   python app.py
   ```
   启动后，默认监听 `http://127.0.0.1:8080`。
   首次启动会自动创建管理员账号，并将一次性随机密码写入服务日志，请首次登录后立即修改。

---

## 🔑 如何获取即梦 Provider 登录 JSON 凭据

为了让平台底层的 Provider 账号能够正常调用即梦生成视频，您需要在 Admin 后台为该 Provider 上传一个真实的即梦 JSON 授权凭据。

**获取该 JSON 的简明完整流程（共 4 步）：**

1. **触发命令**：
   打开系统的命令行终端（如 cmd/PowerShell），执行以下命令：
   ```bash
   dreamina login --headless
   # 如果想强制重新获取，可执行 dreamina relogin --headless
   ```

2. **提取专属链接**：
   命令执行后，在命令行的输出日志中，找到类似下面结构的长链接并复制：
   `https://jimeng.jianying.com/dreamina/cli/v1/dreamina_cli_login?...`

3. **网页登录授权**：
   打开电脑浏览器，先访问并登录即梦官网：
   👉 [https://jimeng.jianying.com/ai-tool/login](https://jimeng.jianying.com/ai-tool/login)

4. **获取最终 JSON**：
   确认即梦网页登录成功后，在**同一个浏览器**中新建一个标签页，粘贴并访问刚才复制的那个长链接。
   此时页面上会直接显示一大段授权 JSON 文本。
   **将这段文本完整复制，保存为 `cookie.json` 文件即可。**

拿到 `cookie.json` 后，您只需登录本系统的 `/admin` 后台，将该文件上传给对应的 Provider，系统调度器将自动接管该账号的算力。凭据只应存放在本机的 `data/provider_cookies/`，不要提交到 Git。

## 🧹 如何一键清空所有历史/卡死任务（重置队列）

如果您在测试期间（例如遇到异常报错导致服务崩溃）发现有大量任务卡在“提交中”等无法消除的状态，您可以**在关闭 Flask 服务器后**，在项目根目录运行以下一行命令，来彻底清空任务列表并重置调度器，同时**完全保留**您的用户账号、积分和 Provider Cookie 授权信息：

```bash
python -c "import sqlite3; conn=sqlite3.connect('data/db.sqlite'); conn.execute('DELETE FROM tasks'); conn.execute('DELETE FROM task_media'); conn.commit()"
```

执行完毕后，重新启动 `python app.py`，系统就又焕然一新了。
