# Source this file from ~/.bashrc.
_agmux_complete() {
    local current="${COMP_WORDS[COMP_CWORD]}" word session
    COMPREPLY=()
    if [[ "${COMP_WORDS[COMP_CWORD-1]}" == --cwd ]]; then
        while IFS= read -r word; do COMPREPLY+=("$word"); done < <(compgen -d -- "$current")
        compopt -o filenames
    elif [[ "${COMP_WORDS[COMP_CWORD-1]}" == --agent ]]; then
        while IFS= read -r word; do COMPREPLY+=("$word"); done < <(
            compgen -W 'codex opencode claude gemini aider crush goose shell' -- "$current"
        )
    elif (( COMP_CWORD == 1 )); then
        while IFS= read -r word; do COMPREPLY+=("$word"); done < <(
            compgen -W 'pick list ls attach new rename shell delete kill usage agents preview peek last detach doctor names --no-preview --no-notify' -- "$current"
        )
    elif (( COMP_CWORD == 2 )) && [[ "${COMP_WORDS[1]}" == @(attach|rename|shell|delete|kill|preview|peek) ]]; then
        while IFS= read -r session; do
            [[ "$session" == "$current"* ]] && COMPREPLY+=("$session")
        done < <(agmux names 2>/dev/null)
    elif [[ "${COMP_WORDS[1]}" == new && "$current" == -* ]]; then
        while IFS= read -r word; do COMPREPLY+=("$word"); done < <(
            compgen -W '--agent --cwd --shell --detach --no-notify --' -- "$current"
        )
    elif [[ "${COMP_WORDS[1]}" == shell && "$current" == -* ]]; then
        while IFS= read -r word; do COMPREPLY+=("$word"); done < <(
            compgen -W '--name --cwd' -- "$current"
        )
    elif [[ "${COMP_WORDS[1]}" == @(delete|kill) && "$current" == -* ]]; then
        while IFS= read -r word; do COMPREPLY+=("$word"); done < <(
            compgen -W '--yes' -- "$current"
        )
    fi
}
complete -F _agmux_complete agmux
# Compatibility: the old `cx` command name still completes.
complete -F _agmux_complete cx 2>/dev/null
