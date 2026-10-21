# 简历修改 Agent

当前为 A-T03 基础工程：React／TypeScript 三栏工作台，连接 Python／FastAPI 的状态接口。

## 本机启动

环境：Node.js 24、Python 3.13。前后端使用两个终端；分别进入对应目录。

### 后端（PowerShell）

```powershell
cd backend
# 首次准备环境时执行；已有 .venv 不必重复创建
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 3001 --workers 1
```

### 前端（PowerShell）

```powershell
cd frontend
npm ci
npm run dev
```

打开 http://127.0.0.1:5173 。前端固定使用 5173，将 `/api` 转发到本机 3001，端口被占用时明确报错，不自动换端口。停止时在对应终端按 Ctrl+C。

## 当前可以检查什么

- 历史会话／对话材料／当前稿三栏基础布局。
- 页面自动获取真实后端状态，支持重新检查、超时与断开提示。
- 求职方向、简历文字、选填 JD 的临时输入。它们只在页面内存中，刷新即清空，不会发送给后端或模型。
- 无密钥启动：`GET /api/health` 返回服务状态及 `model_configured` 布尔值，不返回密钥，不调用模型。

会话保存、文件导入、Agent 分析、建议确认、Markdown 成稿及导出尚未实现，相关按钮明确禁用。数据库业务与模型效果未验证；页面可打开不代表这些功能已完成。

## 配置

后端当前只读取进程环境变量 `DEEPSEEK_API_KEY`；缺失时模型配置状态为 false。`backend/.env.example` 仅说明配置项，复制为 `.env` 不会自动生效。当前工程没有真实模型调用入口，不需要配置密钥即可检查页面。

`.venv`、`.env`、缓存、`.local`、前端依赖和构建目录忽略提交；依赖清单／锁文件和源码可纳入版本管理。学习、分工及计划文档仅本地保留。

## 基础检查

```powershell
cd frontend
npm run typecheck
npm run build
```

后端启动后访问 http://127.0.0.1:3001/api/health ，或通过前端 http://127.0.0.1:5173/api/health 检查代理。配置状态仅表示是否提供非空密钥，不验证凭证、余额或数据库保存能力。
