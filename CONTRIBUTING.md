# Contributing

[中文见下方](#参与贡献)

Thank you for helping. This project is maintained by one person in spare time: replies can take a while, and a feature that does not fit the project's direction may be declined even if it is well made.

## Before you start

- **Bugs:** open an issue with the Blender version, your system, the steps, what happened and what Rhino does in the same case. For interaction problems (clicks, drags, keys), turn on **Debug Log** in the extension's preferences, repeat the steps and attach `r2b_debug.log` from your system temp folder.
- **Features and changes of behavior:** open an issue first and wait for an answer before writing code. Small fixes can go straight to a pull request.

## Rules for the code

1. **Turning Rhino Mode off puts Blender back exactly as it was.** Whatever a feature registers, mutes or changes, its `unregister()` takes back.
2. **Nothing is written to Blender's preferences, themes or keymaps.** Shortcuts go into the add-on keyconfig; built-in items that clash are switched off in memory with the helpers in `keymaps.py`.
3. **No `exec` or `eval`, no network access, no new dependencies.** (These are also conditions of Blender's extensions platform.)
4. **Rhino is the reference for behavior.** When you add or change a command, link the page of Rhino's documentation that describes it. Do not paste text from that documentation, and do not add McNeel's icons, logos or screenshots of Rhino.
5. **Blender 4.2 LTS and the current release both work.** Where their APIs differ, check for the feature (`hasattr`), not the version number.
6. One feature per module, with `register()` and `unregister()`, listed in `mode.py`. Commands that pick points build on `getpoint.GetPoints`.
7. Follow the style of the code around you. Do not reformat lines you are not changing.

## Checking your change

The regression suite is not part of this repository; the maintainer runs it before merging. In the pull request, describe what you checked by hand:

- on Blender 4.2 LTS **and** the current release;
- the steps, and what you saw (a screenshot or a short recording for anything visual);
- that Blender is back to normal after turning Rhino Mode off, if you touched keymaps, handlers, the workspace or anything registered.

To work on the code, add the `src` folder of your clone as a local extension repository (**Preferences > Get Extensions > Repositories > + > Add Local Repository**), then turn the extension off and on to load your changes.

## Pull requests

- One change per pull request, against `main`.
- Say what the change does for the user, not only what the code does.
- The maintainer may edit your branch, squash the commits, or ask for changes.

## License of contributions

By submitting a contribution you agree that it is licensed under **GPL-3.0-or-later**, like the rest of the project, and you confirm that you have the right to submit it: it is your own work, or code under a GPL-compatible license with its origin stated. New source files start with the line `# SPDX-License-Identifier: GPL-3.0-or-later`.

## AI-assisted contributions

Welcome, on the same terms as any other: you understand the code you submit, you have tested it as described above, and you answer for it. Say in the pull request that an assistant was used. Changes that were plainly generated and not tried will be closed.

## Security

Please do not report security problems in public issues. Use **Security > Report a vulnerability** on the repository page.

---

# 参与贡献

感谢你的帮助。这个项目由一个人业余维护：回复可能比较慢；不符合项目方向的功能，即使做得很好也可能不会合并。

## 动手之前

- **问题（bug）：** 开一个 issue，写明 Blender 版本、系统、操作步骤、实际结果，以及同样情况下 Rhino 是怎么做的。交互类的问题（点击、拖动、按键），请在扩展的偏好设置里打开 **Debug Log**，重现一遍，再附上系统临时目录里的 `r2b_debug.log`。
- **新功能和行为上的改动：** 先开 issue 讨论，等有回复再写代码。小的修复可以直接提 PR。

## 代码的规则

1. **关掉 Rhino 模式后，Blender 必须完全恢复原样。** 一个功能注册、屏蔽或改动了什么，它的 `unregister()` 就要全部还原。
2. **不写 Blender 的偏好设置、主题和键位。** 快捷键加到插件的 keyconfig 里；有冲突的内置键位用 `keymaps.py` 里的辅助类在内存中屏蔽。
3. **不用 `exec`、`eval`，不联网，不加新的依赖。**（这些也是 Blender 扩展平台的要求。）
4. **行为以 Rhino 为准。** 新增或修改命令时，请附上 Rhino 文档里对应的页面链接。不要粘贴文档原文，也不要加入 McNeel 的图标、标志或 Rhino 的截图。
5. **Blender 4.2 LTS 和当前版本都要能用。** 两个版本 API 不同的地方，按功能是否存在来判断（`hasattr`），不要按版本号。
6. 一个功能一个模块，提供 `register()` 和 `unregister()`，并列在 `mode.py` 里。需要取点的命令继承 `getpoint.GetPoints`。
7. 跟随周围代码的写法。不要重排没有改动的行。

## 检查你的改动

回归测试不在这个仓库里，维护者会在合并前运行。请在 PR 里写明你手动检查了什么：

- 在 Blender 4.2 LTS **和** 当前版本上都试过；
- 操作步骤和看到的结果（界面上的改动请附截图或短录屏）；
- 如果动了键位、处理器、工作区或任何注册的东西：关掉 Rhino 模式后 Blender 恢复正常。

开发时，把克隆下来的 `src` 文件夹加为本地扩展仓库（**偏好设置 > 获取扩展 > Repositories > + > Add Local Repository**），之后把扩展关掉再打开就能载入改动。

## 提交 PR

- 一个 PR 只做一件事，目标分支是 `main`。
- 说明这个改动对用户意味着什么，不只是代码改了什么。
- 维护者可能会直接修改你的分支、合并提交，或者请你修改。

## 贡献内容的许可

提交贡献即表示你同意它和项目其余部分一样以 **GPL-3.0-or-later** 授权，并确认你有权提交：它是你自己写的，或者是 GPL 兼容许可下的代码并注明了来源。新的源文件第一行写 `# SPDX-License-Identifier: GPL-3.0-or-later`。

## AI 辅助的贡献

欢迎，要求和其他贡献一样：你理解自己提交的代码，按上面的方法测试过，并对它负责。请在 PR 里说明用了 AI 助手。明显是生成后没有试过的改动会被直接关闭。

## 安全问题

请不要在公开的 issue 里报告安全问题，请用仓库页面的 **Security > Report a vulnerability**。
