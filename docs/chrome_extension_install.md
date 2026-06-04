# Chrome 插件安装说明

插件目录：

```text
browser-extension/
```

## 安装步骤

1. 启动 LiveTranslate 客户端。
2. 打开 Chrome，进入：

```text
chrome://extensions/
```

3. 打开右上角“开发者模式”。
4. 点击“加载已解压的扩展程序”。
5. 选择项目里的 `browser-extension/` 目录。

## 使用方式

1. 在网页中选中文字。
2. 右键选择：
   - `AI 翻译解释`
   - `自然翻译`
   - `学术润色`
3. 插件会调用本地接口：

```text
POST http://127.0.0.1:17891/api/translate-selection
```

插件不保存 API Key。API Key 只保存在本地客户端配置中。

## 本地 API Token

如果客户端“设置 → 翻译 → 本地 API Token”或 `config.yaml` 配置了本地 token：

```yaml
local_api:
  token: "your-token"
```

打开插件 popup，在“本地 API Token”输入同一个值并保存。插件后续请求会自动发送：

```text
X-LiveTranslate-Token: your-token
```

如果客户端未配置 token，插件 popup 里的 token 也可以留空。

## 常见问题

### 提示本地客户端未启动

确认 LiveTranslate 正在运行，并且本地接口可访问：

```text
http://127.0.0.1:17891/api/health
```

健康检查会返回 `token_required`，如果为 `true`，插件 popup 中必须填写客户端同一个本地 API Token。

### 修改插件代码后没有生效

进入 `chrome://extensions/`，点击该插件卡片上的“刷新”按钮。
