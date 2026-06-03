# Shell completion

**This document is for:** tab-completing `ietf-llm` commands, flags, and cached corpus names in your
shell. — Back to the [docs index](README.md).

Optional. Add the line for your shell to its rc file:

```bash
# bash — in ~/.bashrc
eval "$(ietf-llm --completion bash)"
```

```bash
# zsh — in ~/.zshrc
eval "$(ietf-llm --completion zsh)"
```

```fish
# fish — in ~/.config/fish/config.fish
ietf-llm --completion fish | source
```
