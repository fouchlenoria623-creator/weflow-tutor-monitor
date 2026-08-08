param([string]$TaskName = 'WeFlow Tutor Monitor')

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) { Unregister-ScheduledTask -TaskName $task.TaskName -Confirm:$false }
