# Talk to type: the speech recogniser used by dualsense_haptics.py --mic.
# It uses Windows' own offline recogniser (System.Speech, English), so nothing is sent anywhere and nothing is
# downloaded. It listens to the Windows default recording device (set it in Sound settings > Input: the DualSense's
# "Headset Microphone", a headset, or any other). The helper talks to it over standard input/output:
#   START  begin listening      STOP  finish (it prints what it heard, then DONE)      QUIT  exit
#   prints: READY, then TEXT<tab>words for every phrase it recognises, and DONE after a STOP.
# -Wave <file.wav> listens to a recorded file instead of the microphone (for testing).
param([string]$Wave = "")

Add-Type -AssemblyName System.Speech
$culture = New-Object System.Globalization.CultureInfo("en-US")
$rec = New-Object System.Speech.Recognition.SpeechRecognitionEngine($culture)
$rec.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
try {
    if ($Wave -ne "") { $rec.SetInputToWaveFile($Wave) } else { $rec.SetInputToDefaultAudioDevice() }
} catch {
    [Console]::Out.WriteLine("ERROR`t" + $_.Exception.Message)
    [Console]::Out.Flush()
    exit 1
}
Register-ObjectEvent -InputObject $rec -EventName SpeechRecognized -SourceIdentifier Spoken | Out-Null

function Send-Line([string]$text) {
    [Console]::Out.WriteLine($text)
    [Console]::Out.Flush()
}
function Drain-Spoken {
    foreach ($e in @(Get-Event -SourceIdentifier Spoken -ErrorAction SilentlyContinue)) {
        $t = $e.SourceEventArgs.Result.Text
        if ($t) { Send-Line ("TEXT`t" + $t) }
        Remove-Event -EventIdentifier $e.EventIdentifier -ErrorAction SilentlyContinue
    }
}

Send-Line "READY"
$task = $null
$listening = $false
while ($true) {
    if ($null -eq $task) { $task = [Console]::In.ReadLineAsync() }
    if ($task.IsCompleted) {
        $line = $task.Result
        $task = $null
        if ($null -eq $line -or $line -eq "QUIT") { break }
        if ($line -eq "START" -and -not $listening) {
            try { $rec.RecognizeAsync([System.Speech.Recognition.RecognizeMode]::Multiple); $listening = $true } catch { Send-Line ("ERROR`t" + $_.Exception.Message) }
        } elseif ($line -eq "STOP") {
            if ($listening) {
                try { $rec.RecognizeAsyncStop() } catch { }
                $listening = $false
            }
            # give the last words a moment to come through
            $until = (Get-Date).AddMilliseconds(1100)
            while ((Get-Date) -lt $until) { Drain-Spoken; Start-Sleep -Milliseconds 50 }
            Drain-Spoken
            Send-Line "DONE"
        }
    }
    Drain-Spoken
    Start-Sleep -Milliseconds 40
}
try { $rec.RecognizeAsyncCancel() } catch { }
$rec.Dispose()
