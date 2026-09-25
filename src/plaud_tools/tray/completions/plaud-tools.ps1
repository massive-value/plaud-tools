# plaud-tools tab completions for PowerShell
# Sourced automatically by the PlaudTools tray app on first run.
# To enable manually: add the following line to your $PROFILE
#   . "<path-to-this-file>"

$_plaud_tools_subcommands = @(
    'list', 'search', 'detail', 'show', 'transcript', 'summary', 'audio',
    'rename', 'folders', 'folder', 'move', 'move-to-folder', 'rename-speaker',
    'correct-transcript', 'correct-summary', 'set-summary', 'transcribe',
    'status', 'trash', 'restore', 'delete', 'trash-move', 'trash-restore',
    'upload', 'merge', 'dump', 'login', 'refresh', 'session', 'update',
    'doctor', 'ping'
)

$_plaud_tools_flags = @{
    'list'                = @('--limit', '--all', '--since', '--until', '--query', '--folder-id', '--unfiled', '--help')
    'search'              = @('--content', '--limit', '--all', '--since', '--until', '--folder-id', '--unfiled', '--help')
    'detail'              = @('--include-transcript', '--help')
    'show'                = @('--help')
    'transcript'          = @('--polish', '--segments', '--help')
    'summary'             = @('--help')
    'audio'               = @('-o', '--output', '--help')
    'rename'              = @('--help')
    'folders'             = @('--help')
    'folder'              = @('create', 'edit', 'delete', '--help')
    'move'                = @('--help')
    'move-to-folder'      = @('--help')
    'rename-speaker'      = @('--help')
    'correct-transcript'  = @('--help')
    'correct-summary'     = @('--help')
    'set-summary'         = @('--content', '--content-file', '--help')
    'transcribe'          = @('--template', '--language', '--diarization', '--no-diarization', '--llm', '--wait', '--help')
    'status'              = @('--help')
    'trash'               = @('--list', '--help')
    'restore'             = @('--help')
    'delete'              = @('--yes', '--help')
    'trash-move'          = @('--help')
    'trash-restore'       = @('--help')
    'upload'              = @('--title', '--folder-id', '--detach', '--skip-summary', '--start-time', '--timezone-offset', '--help')
    'merge'               = @('--title', '--help')
    'dump'                = @('--help')
    'login'               = @('--email', '--password', '--region', '--help')
    'refresh'             = @('--email', '--password', '--region', '--help')
    'session'             = @('show', 'set', 'clear', '--help')
    'update'              = @('--help')
    'doctor'              = @('--help')
    'ping'                = @('--help')
}

$_plaud_tools_folder_subcommands = @('create', 'edit', 'delete')

$_plaud_tools_folder_flags = @{
    'create' = @('--color', '--icon', '--help')
    'edit'   = @('--name', '--color', '--icon', '--help')
    'delete' = @('--yes', '--help')
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
        } elseif ($subcommand -eq 'folder' -and $null -eq $nestedSubcommand -and $_plaud_tools_folder_subcommands -contains $val) {
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
    } elseif ($subcommand -eq 'folder' -and $null -eq $nestedSubcommand) {
        $candidates = $_plaud_tools_flags['folder']
    } elseif ($subcommand -eq 'folder' -and $null -ne $nestedSubcommand) {
        $candidates = $_plaud_tools_folder_flags[$nestedSubcommand]
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
