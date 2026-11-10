# CowAgent Desktop

Cross-platform desktop client for CowAgent, built with Electron + React + TypeScript.

## Windows 中文启动入口

在仓库根目录运行 `.\start-personal.cmd`，即可从源码启动 Electron 与 Python；本机需已有 Python 3.11 和 Node.js 18+，缺失的项目依赖由脚本自动补齐。React 热更新使用 `.\start-personal.cmd -Dev`。参数、数据目录、日志和停止方式见[本地启动说明](../docs/codex/本地启动.md)。

## Development

### Prerequisites

- Node.js 18+
- npm or yarn
- Python 3.7+ (for the backend)

### Setup

```bash
cd desktop
npm install
```

### Run in Development

Start the renderer dev server and Electron together:

```bash
npm run dev
```

Or run them separately:

```bash
# Terminal 1: Start Vite dev server
npm run dev:renderer

# Terminal 2: Start Electron (after renderer is ready)
npm run dev:main
```

The app will automatically start the Python backend from the parent directory.

### Build

```bash
# Build for current platform
npm run dist

# Build for macOS only
npm run dist:mac

# Build for Windows only
npm run dist:win
```

Build outputs are placed in the `release/` directory.

## Architecture

```
desktop/
├── src/
│   ├── main/              # Electron main process
│   │   ├── index.ts       # Window management, IPC
│   │   ├── python-manager.ts  # Python backend lifecycle
│   │   └── preload.ts     # Context bridge for renderer
│   └── renderer/          # React UI (Vite)
│       └── src/
│           ├── api/       # HTTP client for backend APIs
│           ├── components/ # Reusable UI components
│           ├── hooks/     # React hooks
│           ├── pages/     # Page components
│           └── types.ts   # TypeScript types
├── resources/             # App icons
├── package.json           # Dependencies and build config
└── vite.config.ts         # Vite config
```

### How it Works

1. Electron main process starts and creates the app window
2. It spawns the Python backend (`app.py`) as a child process
3. The React UI communicates with the Python backend via HTTP APIs
4. SSE (Server-Sent Events) is used for streaming chat responses and live logs
