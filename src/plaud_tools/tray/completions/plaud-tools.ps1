# plaud-tools tab completions for PowerShell
# Sourced automatically by the PlaudTools tray app on first run.
# To enable manually: add the following line to your $PROFILE
#   . "<path-to-this-file>"

$_plaud_tools_subcommands = @(
    'list', 'search', 'detail', 'show', 'transcript', 'summary', 'audio', 'export',
    'rename', 'folders', 'folder', 'vocab', 'share', 'move', 'move-to-folder', 'rename-speaker',
    'correct-transcript', 'correct-summary', 'set-summary', 'transcribe',
    'resummarize', 'add-note', 'templates', 'models', 'status', 'trash', 'restore', 'delete', 'trash-move', 'trash-restore',
    'upload', 'merge', 'trim', 'dump', 'login', 'refresh', 'session', 'update',
    'doctor', 'ping'
)

$_plaud_tools_flags = @{
    'list'                = @('--limit', '--all', '--since', '--until', '--query', '--folder-id', '--unfiled', '--help')
    'search'              = @('--content', '--limit', '--all', '--since', '--until', '--folder-id', '--unfiled', '--help')
    'detail'              = @('--include-transcript', '--raw', '--help')
    'show'                = @('--help')
    'transcript'          = @('--polish', '--segments', '--help')
    'summary'             = @('--raw', '--help')
    'audio'               = @('-o', '--output', '--help')
    'export'              = @('-f', '--format', '-o', '--output', '--overwrite', '--polish', '--no-speakers', '--no-timestamps', '--expect-fingerprint', '--help')
    'rename'              = @('--help')
    'folders'             = @('--help')
    'folder'              = @('create', 'edit', 'move', 'nest-colon-names', 'delete', '--help')
    'vocab'               = @('list', 'add', 'edit', 'delete', '--help')
    'share'               = @('create', 'get', 'update', 'revoke', '--help')
    'move'                = @('--help')
    'move-to-folder'      = @('--help')
    'rename-speaker'      = @('--help')
    'correct-transcript'  = @('--help')
    'correct-summary'     = @('--note-id', '--help')
    'set-summary'         = @('--content', '--content-file', '--note-id', '--help')
    'transcribe'          = @('--template', '--template-kind', '--llm', '--instruction', '--verbosity', '--language', '--diarization', '--no-diarization', '--wait', '--help')
    'resummarize'         = @('--template', '--template-kind', '--llm', '--instruction', '--verbosity', '--detach', '--yes', '--help')
    'add-note'            = @('--template', '--template-kind', '--llm', '--instruction', '--name', '--detach', '--help')
    'templates'           = @('--help')
    'models'              = @('--help')
    'status'              = @('--help')
    'trash'               = @('--list', '--limit', '--all', '--since', '--until', '--query', '--help')
    'restore'             = @('--help')
    'delete'              = @('--yes', '--help')
    'trash-move'          = @('--help')
    'trash-restore'       = @('--help')
    'upload'              = @('--title', '--folder-id', '--attach-to', '--detach', '--skip-summary', '--start-time', '--timezone-offset', '--help')
    'merge'               = @('--title', '--help')
    'trim'                = @('--help')
    'dump'                = @('--help')
    'login'               = @('--email', '--password', '--region', '--help')
    'refresh'             = @('--email', '--password', '--region', '--help')
    'session'             = @('show', 'set', 'clear', '--help')
    'update'              = @('--help')
    'doctor'              = @('--help')
    'ping'                = @('--help')
}

# Commands with their own subcommands (other than session): subcommand -> flags.
$_plaud_tools_nested = @{
    'folder' = @{
        'create'           = @('--color', '--icon', '--parent-id', '--help')
        'edit'             = @('--name', '--color', '--icon', '--help')
        'move'             = @('--help')
        'nest-colon-names' = @('--yes', '--help')
        'delete'           = @('--yes', '--help')
    }
    'vocab'  = @{
        'list'   = @('--help')
        'add'    = @('--heard-as', '--language', '--help')
        'edit'   = @('--word', '--heard-as', '--language', '--help')
        'delete' = @('--yes', '--help')
    }
    'share'  = @{
        'create' = @('--days', '--never-expires', '--audio', '--no-audio', '--transcript', '--no-transcript', '--summary', '--no-summary', '--yes', '--help')
        'get'    = @('--help')
        'update' = @('--days', '--never-expires', '--audio', '--no-audio', '--transcript', '--no-transcript', '--summary', '--no-summary', '--yes', '--help')
        'revoke' = @('--yes', '--help')
    }
}

$_plaud_tools_session_subcommands = @('show', 'set', 'clear')

$_plaud_tools_session_flags = @{
    'show'  = @('--show-token', '--help')
    'set'   = @('--token', '--region', '--email', '--help')
    'clear' = @('--help')
}

$_plaud_tools_completer = {
    param($wordToComplete, $commandAst, $cursorPosition)

    $tokens = $commandAst.CommandElements
    $subcommand = $null
    $nestedSubcommand = $null

    foreach ($token in ($tokens | Select-Object -Skip 1)) {
        $val = $token.Value
        if ($null -eq $subcommand -and $_plaud_tools_subcommands -contains $val) {
            $subcommand = $val
        } elseif ($subcommand -eq 'session' -and $null -eq $nestedSubcommand -and $_plaud_tools_session_subcommands -contains $val) {
            $nestedSubcommand = $val
        } elseif ($_plaud_tools_nested.ContainsKey($subcommand) -and $null -eq $nestedSubcommand -and $_plaud_tools_nested[$subcommand].ContainsKey($val)) {
            $nestedSubcommand = $val
        }
    }

    $candidates = @()

    if ($null -eq $subcommand) {
        $candidates = $_plaud_tools_subcommands + @('--version', '--help')
    } elseif ($subcommand -eq 'session' -and $null -eq $nestedSubcommand) {
        $candidates = $_plaud_tools_flags['session']
    } elseif ($subcommand -eq 'session' -and $null -ne $nestedSubcommand) {
        $candidates = $_plaud_tools_session_flags[$nestedSubcommand]
    } elseif ($_plaud_tools_nested.ContainsKey($subcommand) -and $null -eq $nestedSubcommand) {
        $candidates = $_plaud_tools_flags[$subcommand]
    } elseif ($_plaud_tools_nested.ContainsKey($subcommand)) {
        $candidates = $_plaud_tools_nested[$subcommand][$nestedSubcommand]
    } else {
        $candidates = $_plaud_tools_flags[$subcommand]
    }

    if ($null -ne $candidates) {
        $candidates | Where-Object { $_ -like "$wordToComplete*" } | ForEach-Object {
            $type = if ($_.StartsWith('-')) { 'ParameterName' } else { 'ParameterValue' }
            [System.Management.Automation.CompletionResult]::new($_, $_, $type, $_)
        }
    }
}

Register-ArgumentCompleter -CommandName plaud-tools -Native -ScriptBlock $_plaud_tools_completer
Register-ArgumentCompleter -CommandName pt -Native -ScriptBlock $_plaud_tools_completer
