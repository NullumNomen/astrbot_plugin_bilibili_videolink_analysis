# AstrBot B 站视频链接解析

在群聊或私聊发送 B 站视频页、短链接或独立的 BV/AV 号，机器人会回复封面、标题、UP 主、简介、互动数据和视频页链接。QQ OneBot v11 适配器也可解析 B 站小程序分享卡片。图文资料来自 B 站公开网页接口。

## 安装

在 AstrBot WebUI 中选择「插件 → 安装插件 → 从文件安装」，上传插件 ZIP。

也可以手动安装源码。

1. 将本目录保存为 `astrbot_plugin_bilibili_videolink_analysis`，放入 AstrBot 的 `data/plugins/`，确保 `main.py` 和 `metadata.yaml` 直接位于插件目录下。
2. 在 AstrBot WebUI 的插件页加载或重载插件。手动部署时，在运行 AstrBot 的 Python 环境下执行 `python -m pip install -r requirements.txt` 安装依赖。
3. 在插件设置里调整触发方式、每条消息的解析上限、重复解析间隔和图文模板。

## 使用

- 普通链接示例：`https://www.bilibili.com/video/BV1ii421Q7oj`。
- 链接带 `?p=2` 时，回复会保留第 2 分 P 的地址。模板中可加入 `${分P}` 显示该分 P 的标题。

## 图文模板

普通链接与 QQ 卡片可分别配置。卡片模板留空时使用普通链接模板。

默认模板如下。升级时仍使用 v0.1.0 默认模板的配置会自动换成新版模板，自定义模板会继续使用。

```text
${封面}
🎬${标题}
📝 ${简介}
👤 UP 主：${UP主}
▶️ 播放 ${观看} · 👍 点赞 ${点赞} · 💬 弹幕 ${弹幕}
🪙 投币 ${投币} · ⭐ 收藏 ${收藏} · 🔄 转发 ${转发}
🔗 ${视频地址}
```

其他可用变量有 `${分P}`、`${BV号}`、`${AV号}` 和 `${播放链接}`。用 `${~~~}` 将模板拆成多条消息。

## 转发格式

在插件设置中打开「以合并转发格式发送」后，OneBot v11 群聊中的图文预览会发为一条合并转发消息，每个模板分条对应一个转发节点。单条预览也适用。开关默认关闭，私聊、其他平台及转发失败时发送普通图文。

## 测试

```sh
python -m unittest discover -s tests
```

本项目基于 [shangxue 的 Koishi 插件](https://github.com/koishi-shangxue-plugins/koishi-shangxue-apps/tree/main/plugins/bilibili-videolink-analysis) 的功能移植，遵循 MIT 许可证。
