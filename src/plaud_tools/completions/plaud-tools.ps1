# plaud-tools tab completions for PowerShell
# Sourced automatically by the PlaudTools tray app on first run.
# To enable manually: add the following line to your $PROFILE
#   . "<path-to-this-file>"

$_plaud_tools_subcommands = @(
    'list', 'search', 'detail', 'show', 'transcript', 'summary',
    'rename', 'folders', 'move-to-folder', 'move', 'rename-speaker',
    'transcribe', 'status', 'trash', 'restore', 'delete',
    'trash-move', 'trash-restore', 'upload', 'merge',
    'login', 'session', 'ping'
)

$_plaud_tools_flags = @{
    'list'           = @('--limit', '--since', '--until', '--query', '--folder-id', '--unfiled', '--help')
    'search'         = @('--limit', '--since', '--until', '--folder-id', '--help')
    'detail'         = @('--include-transcript', '--help')
    'show'           = @('--help')
    'transcript'     = @('--help')
    'summary'        = @('--help')
    'rename'         = @('--help')
    'folders'        = @('--help')
    'move-to-folder' = @('--help')
    'move'           = @('--help')
    'rename-speaker' = @('--help')
    'transcribe'     = @('--template', '--help')
    'status'         = @('--help')
    'trash'          = @('--help')
    'restore'        = @('--help')
    'delete'         = @('--yes', '--help')
    'trash-move'     = @('--help')
    'trash-restore'  = @('--help')
    'upload'         = @('--title', '--folder-id', '--detach', '--help')
    'merge'          = @('--title', '--help')
    'login'          = @('--email', '--password', '--region', '--help')
    'session'        = @('show', 'set', 'clear', '--help')
    'ping'           = @('--help')
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
    $sessionSubcommand = $null

    foreach ($token in ($tokens | Select-Object -Skip 1)) {
        $val = $token.Value
        if ($null -eq $subcommand -and $_plaud_tools_subcommands -contains $val) {
            $subcommand = $val
        } elseif ($subcommand -eq 'session' -and $null -eq $sessionSubcommand -and $_plaud_tools_session_subcommands -contains $val) {
            $sessionSubcommand = $val
        }
    }

    $candidates = @()

    if ($null -eq $subcommand) {
        $candidates = $_plaud_tools_subcommands + @('--version', '--help')
    } elseif ($subcommand -eq 'session' -and $null -eq $sessionSubcommand) {
        $candidates = $_plaud_tools_flags['session']
    } elseif ($subcommand -eq 'session' -and $null -ne $sessionSubcommand) {
        $candidates = $_plaud_tools_session_flags[$sessionSubcommand]
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
