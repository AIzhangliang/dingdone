# 触发器协议 TRIGGER_API.md

# 完工铃 · AI 主动播报接入文档（trigger.json 协议）

> 适用于：Claude Code / Codex / Cursor / 任何能写文件的 AI Agent / 你自己的脚本
> 版本：v2.2.2+

## 一、原理

完工铃每 10 秒检查一次它旁边的 `trigger.json`。任何程序往这个文件写入一条 JSON，
完工铃就会在下一次检查时读出内容并按事件规则播报。

## 二、触发文件格式

```json
{
  "event": "task_done",
  "text": "你想让铃说的话"
}
```

- `event`：事件类型，决定用哪个声音（可选值见下）
- `text`：播报内容（最多 100 字，超出截断）

## 三、事件类型

| event | 默认声音 | 用途 |
|---|---|---|
| task_done | 音效+语音 | 任务完成（最常用） |
| heartbeat | 音效 | 心跳/异常提醒 |
| （其他）| 按该事件配置 | 自定义扩展 |

## 四、接入方法

### Claude Code / Codex 用户
在你的 CLAUDE.md / AGENTS.md 里加一句：

> 当完成任务时，向 `完工铃所在目录/trigger.json` 写入
> `{"event":"task_done","text":"<任务名>完成，用时 <N> 分钟"}`（JSON 格式）

之后每次 AI 干完活，完工铃自动语音播报。

### 你自己的脚本（Python 示例）

```python
import json
json.dump({"event": "task_done", "text": "数据抓取完成，共 42 条"},
          open(r"C:\软件\完工铃\trigger.json", "w", encoding="utf-8"), ensure_ascii=False)
```

### 任意程序
写一个 JSON 文本文件到 `trigger.json` 即可——没有语言限制。

## 五、注意事项

1. `text` 控制在 100 字内（语音播报超长会截断）
2. 写入频率不限，但建议同一个任务只触发一次
3. 免打扰时段（23:00-8:00）内触发只记录不发声
4. 触发器是本机文件——**不要把完工铃目录放在公共网络路径**（任何能写该目录的
   程序都能触发播报）

## 六、版本

- v2.2.2：协议首个公开版本
