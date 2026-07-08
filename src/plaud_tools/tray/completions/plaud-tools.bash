# plaud-tools bash completions
# Source this file to enable tab-completion for plaud-tools and pt:
#   source /path/to/plaud-tools.bash
# Or add the above line to your ~/.bashrc

_plaud_tools_complete() {
    local cur prev words cword
    _init_completion 2>/dev/null || {
        cur="${COMP_WORDS[COMP_CWORD]}"
        prev="${COMP_WORDS[COMP_CWORD-1]}"
        words=("${COMP_WORDS[@]}")
        cword=$COMP_CWORD
    }

    local subcommands="list search detail show transcript summary rename folders move-to-folder move rename-speaker transcribe status trash restore delete trash-move trash-restore upload merge login session ping"

    if [[ $cword -eq 1 ]]; then
        COMPREPLY=($(compgen -W "$subcommands --version --help" -- "$cur"))
        return
    fi

    local subcmd="${words[1]}"
    case "$subcmd" in
        list)
            COMPREPLY=($(compgen -W "--limit --since --until --query --folder-id --unfiled --help" -- "$cur"))
            ;;
        search)
            COMPREPLY=($(compgen -W "--limit --since --until --folder-id --help" -- "$cur"))
            ;;
        detail)
            COMPREPLY=($(compgen -W "--include-transcript --help" -- "$cur"))
            ;;
        transcribe)
            COMPREPLY=($(compgen -W "--template --help" -- "$cur"))
            ;;
        delete)
            COMPREPLY=($(compgen -W "--yes --help" -- "$cur"))
            ;;
        upload)
            COMPREPLY=($(compgen -W "--title --folder-id --detach --help" -- "$cur"))
            ;;
        merge)
            COMPREPLY=($(compgen -W "--title --help" -- "$cur"))
            ;;
        login)
            COMPREPLY=($(compgen -W "--email --password --region --help" -- "$cur"))
            ;;
        session)
            if [[ $cword -eq 2 ]]; then
                COMPREPLY=($(compgen -W "show set clear --help" -- "$cur"))
            elif [[ "${words[2]}" == "show" ]]; then
                COMPREPLY=($(compgen -W "--show-token --help" -- "$cur"))
            elif [[ "${words[2]}" == "set" ]]; then
                COMPREPLY=($(compgen -W "--token --region --email --help" -- "$cur"))
            fi
            ;;
        *)
            COMPREPLY=($(compgen -W "--help" -- "$cur"))
            ;;
    esac
}

complete -F _plaud_tools_complete plaud-tools pt
