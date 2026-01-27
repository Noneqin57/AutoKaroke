# 🎤 AutoKaraoke (AutoLRC Pro)

**AutoKaraoke** 是一个基于 OpenAI [Whisper](https://github.com/openai/whisper) 的本地化自动歌词制作与打轴工具。它利用强大的 AI 模型自动识别语音、生成 LRC/SRT 字幕，并提供智能的手动校准功能，旨在简化卡拉 OK 歌词（KRC/LRC）的制作流程。
这是一个单脚本，后续已重构至AutoKaraoke Refactored(https://github.com/Noneqin57/AutoKaraoke-Refactored)
⚠️ **注**：本项目核心逻辑由 AI 辅助生成，目前处于 v0.1 测试阶段。

## ✨ 核心功能

### 1. 🤖 强大的 AI 识别与对齐
* **双模式支持**：
    * **听写模式**：没有歌词文本？AI 自动听写并生成带时间轴的歌词。
    * **强制对齐模式 (Alignment)**：已有歌词文本？导入文本，AI 会将其与音频进行毫秒级强制对齐，准确率极高。
* **多引擎支持**：内置 `Faster-Whisper`（速度快）和 `Stable-Whisper`（时间轴稳）双引擎，自动根据环境切换。
* **多语言支持**：支持中文、日语、英语、韩语、粤语等多语言识别。
* **自定义 Prompt**：支持输入提示词（如“这是一首粤语歌”），引导 AI 更准确地识别风格和歌词内容。

### 2. 🛠️ 智能手动校准编辑器
AI 生成的轴不满意？内置编辑器帮你快速修正：
* **可视化操作**：提供播放进度条、时间戳列表，支持空格键播放/暂停。
* **智能同步写入**：
    * **一键打轴**：听到歌词时按下 `Enter` 键，即可将当前播放时间写入该行。
    * **智能防撞与空隙修复**：修改行时间时，程序会自动计算偏移量，智能修正行内每个字的间距，防止时间轴重叠或产生异常空隙。
    * **双语同步**：自动识别并同步更新对应的翻译行时间，无需重复打轴。

### 3. ⚡ 本地化运行
* 无需上传文件，所有处理在本地完成，保护隐私。
* 支持 NVIDIA GPU 加速，处理速度可达实时的 10 倍以上。

---

## 🚧 开发计划 (To-Do)

目前已实现：
- [x] 基于原有歌词文本的行/字级强制对齐 
- [x] 带波形预览的手动歌词校准器 
- [x] 自定义 Prompt 提示词

未来计划：
- [ ] 更准确的纯音频逐字歌词生成（脱离参考文本） 
- [ ] 集成 UVR5 等人声分离功能，提高伴奏干扰下的识别率 
- [ ] 自动歌词配对功能（自动匹配原文与翻译） 

---

## 💻 安装指南

### 1. 环境要求
* **操作系统**：Windows / macOS / Linux
* **Python**：建议版本 3.10 或更高
* **FFmpeg**：程序处理音频必须依赖 FFmpeg。
    * *Windows*: [下载 FFmpeg](https://www.gyan.dev/ffmpeg/builds/) 并配置环境变量。
    * *Mac*: `brew install ffmpeg`

### 2. 安装依赖
请确保安装了以下 Python 库：

```bash
pip install PyQt6 torch torchaudio stable-whisper faster-whisper openai-whisper

```

> **💡 GPU 加速提示**：
> 如果你拥有 NVIDIA 显卡，强烈建议安装 CUDA 版本的 PyTorch 以获得极速体验：
> ```bash
> pip3 install torch torchvision torchaudio --index-url [https://download.pytorch.org/whl/cu121](https://download.pytorch.org/whl/cu121)
> 
> ```
> 
> 

### 3. 运行程序

下载本项目代码，在终端运行：

```bash
python main.py

```

---

## 📖 使用教程

1. **加载音频**：点击“选择歌曲”，支持 mp3/wav/flac 等常见格式。
2. **导入歌词 (推荐)**：
* 如果你有歌词文本（`.lrc` 或 `.txt`），点击“导入 LRC/TXT”。
* 程序会自动识别原文和翻译行。
* **提示**：提供参考文本会让时间轴非常精准（强制对齐模式）。


3. **配置参数**：
* **模型**：推荐使用 `large-v2` 或 `large-v3` 以获得最佳精度。
* **语言**：选择歌曲对应的语言。
* **Prompt**：如果是特殊风格（如 Rap），可输入“这是一首快节奏的中文说唱”来引导 AI。


4. **开始生成**：点击“开始生成”，等待进度条完成。
5. **人工校准**：
* 点击右侧的“校准/编辑”按钮。
* **双击**某一行可跳转播放。
* **Enter 键**：将当前播放进度写入选中行（核心功能）。


6. **保存**：点击“保存结果”导出最终的 LRC 文件。

---

## ❓ 常见问题

**Q: 为什么提示 `Could not load library zlibwapi.dll`？**
A: 这是 Windows 下使用 Faster-Whisper 的常见问题。请下载 `zlibwapi.dll` 并将其放入 `C:\Windows\System32` 文件夹中。

**Q: 没有 GPU 可以运行吗？**
A: 可以，程序会自动切换到 CPU 模式，但速度会慢很多，建议使用 `small` 或 `medium` 模型。

---

## 📜 开源协议

本项目遵循 MIT 开源协议。

#

