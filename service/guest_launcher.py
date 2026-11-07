"""Le `.bat` autonome de l'invité : un fichier, sans archive, fonction pure de ses entrées.

D-G : le `.bat` porte chaque fichier du mod en base64, avec son chemin relatif et son SHA-256,
triés par nom. Ni zip ni `Expand-Archive` : les octets ne dépendent d'aucune version de zlib.
La logique est du PowerShell 5.1, relue dans le `.bat` par `$env:PZDUEL_SELF`, sans
`-EncodedCommand`.

D-H : mot de passe et adresse passent par une liste blanche, et tout le reste est refusé sans
citer la valeur. Rien n'est échappé : aucune valeur admise ne porte de caractère que `cmd` ou
PowerShell interpréterait. Elles voyagent en plus sur une ligne de données, hors du texte du
script : celui-ci est identique pour tous les duels, et le mot de passe ne figure pas dans ce
que la journalisation des blocs de script de Windows conserverait.

Disposition : `cmd` lit l'en-tête et s'arrête à `exit /b`. Tout ce qui suit n'est lu que par
PowerShell, entre des marqueurs qui occupent seuls leur ligne.

Code de vérification : le `.bat` voyage en `http://`, un intermédiaire peut donc le remplacer. Le
code est le début du SHA-256 de tout le fichier, lu à l'oral par l'hôte. Il ne peut pas figurer dans
le fichier, qui le ferait changer. Il est en hexadécimal pour que l'invité le compare AVANT d'ouvrir
le fichier, avec `certutil` : c'est le seul contrôle qui tienne contre un attaquant. Celui du `.bat`
s'exécute dans le fichier contrôlé, et un `.bat` substitué n'a qu'à l'omettre ; il n'arrête qu'un
fichier altéré qui garde ce contrôle, et le code n'est jamais affiché par le lanceur.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import re
from pathlib import Path

CRLF = "\r\n"
# `cmd` coupe une ligne à 8191 caractères ; AC30 fixe le plafond du format.
MAX_LINE_BYTES = 8000
MAX_LAUNCHER_BYTES = 256 * 1024
BASE64_LINE_CHARS = 6000
MAX_PORT = 65535
# 64 bits : un faux fichier au même code coûte de l'ordre de 2^64 calculs, hors de portée d'un
# attaquant pendant la vie d'un lobby. Le PowerShell écrit la même valeur dans `$CodeChars`.
LAUNCHER_CODE_CHARS = 16
# Le client doit apparaître, puis tourner encore `CLIENT_SETTLE_S` secondes, dans les
# `CLIENT_START_S` secondes qui suivent son lancement ; sinon, le lancement a échoué. Le PowerShell
# écrit les mêmes valeurs dans `$StartSeconds` et `$SettleSeconds`.
CLIENT_START_S = 30
CLIENT_SETTLE_S = 5
# Le mod remplacé est gardé dans `Zomboid`, hors de `mods` : le jeu y verrait sinon deux mods de
# même identifiant.
PREVIOUS_SUFFIX = ".previous"

PASSWORD_ALLOWED = re.compile(r"[A-Za-z0-9._-]+")
HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?")
MAX_HOST_CHARS = 253
MAX_LABEL_CHARS = 63
LOOPBACK_NAME = "localhost"

ADDRESS_REFUSED = "address refused: it must be an IPv4 address or a host name guests can reach"
PORT_REFUSED = "port refused: it must be a whole number from 1 to 65535"
PASSWORD_REFUSED = "password refused: only letters, digits, '.', '_' and '-' are allowed"
MOD_REFUSED = "mod refused: the folder holds no file"
MOD_UNREADABLE = "mod refused: its files could not be read"
FORMAT_REFUSED = "launcher refused: it would not fit the size limits of a .bat file"

SCRIPT_MARK = "::PZDUEL-SCRIPT"
DATA_MARK = "::PZDUEL-DATA"
JOIN_MARK = "::PZDUEL-JOIN"
MOD_MARK = "::PZDUEL-MOD"
FILE_MARK = "::PZDUEL-FILE"

# Le chemin complet : `cmd` essaierait sinon `powershell.exe` dans le dossier de téléchargement.
# Pas de `%` dans la commande, et rien que des apostrophes dedans : elle est entre guillemets.
# `-ExecutionPolicy Bypass` ne vaut que pour ce processus et ses enfants : rien n'est écrit dans le
# registre, aucun réglage ne survit, et une stratégie de groupe l'emporte. Il n'y a rien à resserrer.
# Il ne protège ni n'expose rien ici : la stratégie vise les fichiers de script, pas `-Command`.
# `PZDUEL_TYPED` est vidé d'abord (`set /p` garderait sinon une valeur héritée sur une ligne vide),
# et jamais développé par `cmd` : la saisie est libre, seul PowerShell la lit, comme une donnée.
BATCH = rf"""@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "PZDUEL_SELF=%~f0"
echo pz-duel: this invitation is meant for a trusted network.
echo The code the host reads to you is the only protection against a replaced file.
echo It must be compared before this file is run: certutil -hashfile "this file" SHA256
echo must print a value that starts with that code. If it was not, close this window now.
echo This launcher asks for that code below. A launcher that does not ask for a code was replaced:
echo never run one, and ask the host for a new file.
set "PZDUEL_TYPED="
set /p "PZDUEL_TYPED=Type the code the host read to you, then press Enter: "
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop';$l=[IO.File]::ReadAllLines($env:PZDUEL_SELF,[Text.Encoding]::ASCII);$b=[Array]::IndexOf($l,'{SCRIPT_MARK}');$e=[Array]::IndexOf($l,'{DATA_MARK}');& ([ScriptBlock]::Create($l[($b+1)..($e-1)] -join [Environment]::NewLine)) $l"
set "PZDUEL_CODE=%ERRORLEVEL%"
if not "%PZDUEL_CODE%"=="0" echo pz-duel: the launcher failed. If no cause is shown above, PowerShell is probably blocked on this PC.
if not "%PZDUEL_CODE%"=="0" pause
exit /b %PZDUEL_CODE%"""

# PowerShell 5.1. `[IO.Path]::Combine` plutôt que `Join-Path`, qui lève sur un disque absent.
# Les chemins restent sans séparateur écrit en dur.
POWERSHELL = r"""param([string[]]$Lines)
$ErrorActionPreference = 'Stop'
$ClientName = 'ProjectZomboid64.exe'
$ModId = 'duelcore'
$Damaged = 'This launcher file is damaged. Ask the host for a new one.'
$CodeChars = 16
$StartSeconds = 30
$SettleSeconds = 5
$PollMs = 250
$ClientProcess = [IO.Path]::GetFileNameWithoutExtension($ClientName)
$NotStarted = 'Project Zomboid did not start, or closed at once. Start it once from Steam to check that it works, then run this file again.'
$CodeRefused = 'This is not the code of this file. Do not use it: ask the host for a new launcher.'

function Test-Code([string]$Path, [string]$Typed) {
    $sha = [Security.Cryptography.SHA256]::Create()
    $hash = [BitConverter]::ToString($sha.ComputeHash([IO.File]::ReadAllBytes($Path)))
    $code = $hash.Replace('-', '').Substring(0, $CodeChars).ToLowerInvariant()
    ($Typed -replace '\s', '').ToLowerInvariant() -ceq $code
}

function Get-Libraries([string]$Steam) {
    try {
        $file = [IO.Path]::Combine($Steam, 'steamapps', 'libraryfolders.vdf')
        if (-not [IO.File]::Exists($file)) { return }
        $text = [IO.File]::ReadAllText($file, [Text.Encoding]::UTF8)
    } catch {
        return
    }
    $depth = 0
    $key = $null
    $found = @()
    foreach ($match in [regex]::Matches($text, '(?s)"(?:[^"\\]|\\.)*"|[{}]|"')) {
        $token = $match.Value
        if ($token -eq '"') { return }
        if ($token -eq '{' -or $token -eq '}') {
            if ($token -eq '{') { $depth++ } else { $depth-- }
            $key = $null
            if ($depth -lt 0) { return }
        } elseif ($null -eq $key) {
            $key = $token.Substring(1, $token.Length - 2)
        } else {
            $value = [regex]::Replace($token.Substring(1, $token.Length - 2), '(?s)\\(.)', '$1')
            $current = $depth -eq 2 -and $key -ieq 'path'
            $legacy = $depth -eq 1 -and $key -match '^[0-9]+$'
            if ($value -and ($current -or $legacy)) { $found += $value }
            $key = $null
        }
    }
    if ($depth -eq 0) { $found }
}

function Get-SteamRoots {
    $default = ${env:ProgramFiles(x86)}
    if ($default) { [IO.Path]::Combine($default, 'Steam') }
    $keys = @(
        @('HKCU:\Software\Valve\Steam', 'SteamPath'),
        @('HKLM:\SOFTWARE\WOW6432Node\Valve\Steam', 'InstallPath')
    )
    foreach ($entry in $keys) {
        try {
            $value = (Get-ItemProperty -LiteralPath $entry[0] -Name $entry[1]).($entry[1])
            if ($value) { $value }
        } catch {
        }
    }
}

function Find-Client {
    $roots = @(Get-SteamRoots)
    $folders = $roots
    foreach ($root in $roots) { $folders += @(Get-Libraries $root) }
    foreach ($folder in $folders) {
        try {
            $client = [IO.Path]::Combine($folder, 'steamapps', 'common', 'ProjectZomboid', $ClientName)
            if ([IO.File]::Exists($client)) { return $client }
        } catch {
        }
    }
}

function Read-Client {
    Write-Host 'Project Zomboid was not found automatically.'
    $answer = $null
    try {
        $answer = Read-Host "Paste the folder that holds $ClientName, then press Enter"
    } catch {
    }
    if ($answer) { $answer = $answer.Trim().Trim('"') }
    if (-not $answer) { throw 'Project Zomboid was not found and no folder was given.' }
    $client = [IO.Path]::Combine($answer, $ClientName)
    if (-not [IO.File]::Exists($client)) { throw "$ClientName is not in the folder: $answer" }
    $client
}

function Read-Join([string[]]$Lines, [int]$Data) {
    $join = $Lines[$Data + 1].Split(' ')
    if ($join.Count -ne 4 -or $join[0] -cne '::PZDUEL-JOIN') { throw $Damaged }
    $join
}

function Write-Mod([string[]]$Lines, [int]$Data, [string]$Root) {
    $entries = New-Object Collections.ArrayList
    $entry = $null
    for ($i = $Data + 3; $i -lt $Lines.Count; $i++) {
        $line = $Lines[$i]
        if ($line.StartsWith('::PZDUEL-FILE ')) {
            $fields = $line.Split(' ')
            $entry = @{ Sum = $fields[1]; Name = $fields[2]; Data = New-Object Text.StringBuilder }
            [void]$entries.Add($entry)
        } elseif ($null -ne $entry) {
            [void]$entry.Data.Append($line)
        }
    }
    $header = $Lines[$Data + 2].Split(' ')
    if ($header.Count -ne 2 -or $header[0] -cne '::PZDUEL-MOD' -or $header[1] -cne [string]$entries.Count) {
        throw $Damaged
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    foreach ($entry in $entries) {
        try {
            $name = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($entry.Name))
            $bytes = [Convert]::FromBase64String($entry.Data.ToString())
        } catch {
            throw $Damaged
        }
        $file = [IO.Path]::Combine($Root, $name)
        [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($file))
        [IO.File]::WriteAllBytes($file, $bytes)
        $hash = $sha.ComputeHash([IO.File]::ReadAllBytes($file))
        $sum = [BitConverter]::ToString($hash).Replace('-', '').ToLowerInvariant()
        if ($sum -cne $entry.Sum) { throw $Damaged }
    }
}

function Start-Client([string]$Client, [string[]]$Join) {
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $Client
    $info.Arguments = '-nosteam +connect ' + $Join[1] + ':' + $Join[2] + ' +password ' + $Join[3]
    $info.WorkingDirectory = [IO.Path]::GetDirectoryName($Client)
    $info.UseShellExecute = $true
    [void][Diagnostics.Process]::Start($info)
}

# The game's processes, by name: the one started may hand over to another, so its object proves nothing.
function Get-ClientIds {
    @(Get-Process | Where-Object {
        [IO.Path]::GetFileNameWithoutExtension($_.ProcessName) -ieq $ClientProcess
    } | ForEach-Object { $_.Id })
}

# True once a game process that was not there before has run for $SettleSeconds.
# One that appears then vanishes is a failure at once; none at all is one after $StartSeconds.
function Wait-Client([int[]]$Before) {
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $seen = $null
    while ($clock.Elapsed.TotalSeconds -lt $StartSeconds) {
        $new = @(Get-ClientIds | Where-Object { $Before -notcontains $_ })
        if ($new.Count -gt 0) {
            if ($null -eq $seen) { $seen = $clock.Elapsed.TotalSeconds }
            if ($clock.Elapsed.TotalSeconds - $seen -ge $SettleSeconds) { return $true }
        } elseif ($null -ne $seen) {
            return $false
        }
        Start-Sleep -Milliseconds $PollMs
    }
    $false
}

function Get-Sums([string]$Folder) {
    $sha = [Security.Cryptography.SHA256]::Create()
    $root = [IO.Path]::GetFullPath($Folder)
    $sums = foreach ($file in [IO.Directory]::GetFiles($root, '*', [IO.SearchOption]::AllDirectories)) {
        $hash = [BitConverter]::ToString($sha.ComputeHash([IO.File]::ReadAllBytes($file)))
        $file.Substring($root.Length) + ' ' + $hash
    }
    (@($sums) | Sort-Object) -join "`n"
}

# The replaced mod goes to Zomboid, outside mods. One that equals the new mod is not kept:
# it would push out the guest's own earlier mod, on a rematch.
function Save-Previous([string]$Previous, [string]$Target, [string]$Backup) {
    if ((Get-Sums $Previous) -ceq (Get-Sums $Target)) {
        Remove-Item -LiteralPath $Previous -Recurse -Force
        if (Test-Path -LiteralPath $Backup) {
            Write-Host "Your earlier $ModId mod is still kept in: $Backup"
        }
        return
    }
    if (Test-Path -LiteralPath $Backup) { Remove-Item -LiteralPath $Backup -Recurse -Force }
    Move-Item -LiteralPath $Previous -Destination $Backup
    Write-Host "Your previous $ModId mod was not deleted. It is kept in: $Backup"
    Write-Host "To go back to it, quit the game, delete $Target and move that folder back in its place, named $ModId."
}

if (-not (Test-Code $env:PZDUEL_SELF $env:PZDUEL_TYPED)) {
    Write-Host ('ERROR: ' + $CodeRefused) -ForegroundColor Red
    Write-Host 'The game was not started and the installed mod was not changed.'
    exit 1
}

$stage = $null
$keepStage = $false
$code = 1
try {
    $data = [Array]::IndexOf($Lines, '::PZDUEL-DATA')
    $join = Read-Join $Lines $data
    $client = Find-Client
    if (-not $client) { $client = Read-Client }
    if (-not $env:USERPROFILE) { throw 'USERPROFILE is not set.' }
    $zomboid = [IO.Path]::Combine($env:USERPROFILE, 'Zomboid')
    $mods = [IO.Path]::Combine($zomboid, 'mods')
    $target = [IO.Path]::Combine($mods, $ModId)
    $backup = [IO.Path]::Combine($zomboid, $ModId + '.previous')
    $stage = [IO.Path]::Combine($zomboid, '.' + $ModId + '-' + [Guid]::NewGuid().ToString('N'))
    $staged = [IO.Path]::Combine($stage, 'new')
    $previous = [IO.Path]::Combine($stage, 'previous')
    $rejected = [IO.Path]::Combine($stage, 'rejected')
    Write-Host 'Checking the duel mod...'
    Write-Mod $Lines $data $staged
    [void][IO.Directory]::CreateDirectory($mods)
    $replacing = Test-Path -LiteralPath $target
    if ($replacing) { Move-Item -LiteralPath $target -Destination $previous }
    $waited = $false
    try {
        Move-Item -LiteralPath $staged -Destination $target
        Write-Host 'Starting Project Zomboid...'
        $before = Get-ClientIds
        Start-Client $client $join
        Write-Host "Checking that the game keeps running ($SettleSeconds seconds)..."
        $waited = $true
        if (-not (Wait-Client $before)) { throw $NotStarted }
    } catch {
        $cause = $_.Exception.Message
        try {
            if (Test-Path -LiteralPath $target) { Move-Item -LiteralPath $target -Destination $rejected }
            if ($replacing) { Move-Item -LiteralPath $previous -Destination $target }
        } catch {
            $keepStage = $true
            throw "$cause The previous mod could not be put back; it is kept in: $previous"
        }
        if ($waited) {
            $keepStage = $true
            throw "$cause The installed mod was not changed. The mod that was tried is kept in: $rejected"
        }
        throw $cause
    }
    $code = 0
    Write-Host 'Project Zomboid is running.'
    if ($replacing) {
        try {
            Save-Previous $previous $target $backup
        } catch {
            $keepStage = $true
            Write-Host "Your previous $ModId mod is kept in: $previous"
        }
    }
} catch {
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    if (-not $keepStage) { Write-Host 'The game was not started and the installed mod was not changed.' }
    Write-Host 'If Project Zomboid is running, close it and run this file again.'
} finally {
    if ($stage -and -not $keepStage -and (Test-Path -LiteralPath $stage)) {
        Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    }
}
exit $code"""


class LauncherRefused(ValueError):
    """Valeur que le `.bat` ne peut pas porter. Le message ne la cite jamais : secret possible."""


def _is_usable_ipv4(text: str) -> bool:
    try:
        address = ipaddress.IPv4Address(text)
    except ValueError:
        return False
    return not (address.is_loopback or address.is_link_local or address.is_unspecified)


def is_usable_address(address: object) -> bool:
    """H7 : une IPv4 ou un nom d'hôte, sans la boucle, l'adresse nulle ni le lien local.

    Un dernier label qui commence par un chiffre n'est pas un nom : `127.1`, `2130706433` ou
    `0x7f.1` désignent la boucle pour le résolveur, et seule une IPv4 complète est admise.
    """
    if not isinstance(address, str):
        return False
    name = address.removesuffix(".")
    labels = name.split(".")
    if len(name) > MAX_HOST_CHARS:
        return False
    if not all(len(item) <= MAX_LABEL_CHARS and HOST_LABEL.fullmatch(item) for item in labels):
        return False
    if labels[-1][0].isdigit():
        return _is_usable_ipv4(address)
    return labels[-1].lower() != LOOPBACK_NAME


def _is_port(port: object) -> bool:
    return isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= MAX_PORT


def read_mod(mod_src: str | os.PathLike[str]) -> tuple[str, ...]:
    """Lit et hache le mod : la seule étape lente du `.bat`, à faire hors de tout verrou.

    Rend les lignes de données que `build_launcher` accepte à la place du dossier, ou lève
    `LauncherRefused`.
    """
    return tuple(_mod_lines(mod_src))


def _mod_lines(mod_src: str | os.PathLike[str]) -> list[str]:
    """Rend le mod en lignes de données, avec les fichiers et l'ordre de `mod_fingerprint`."""
    root = Path(mod_src)
    files = {path.relative_to(root).as_posix(): path for path in root.rglob("*") if path.is_file()}
    if not files:
        raise LauncherRefused(MOD_REFUSED)
    lines = [f"{MOD_MARK} {len(files)}"]
    for name in sorted(files):
        try:
            content = files[name].read_bytes()
        except OSError as failure:
            raise LauncherRefused(MOD_UNREADABLE) from failure
        encoded_name = base64.b64encode(name.encode("utf-8")).decode("ascii")
        lines.append(f"{FILE_MARK} {hashlib.sha256(content).hexdigest()} {encoded_name}")
        encoded = base64.b64encode(content).decode("ascii")
        lines += [
            encoded[start : start + BASE64_LINE_CHARS]
            for start in range(0, len(encoded), BASE64_LINE_CHARS)
        ]
    return lines


def launcher_code(launcher: bytes) -> str:
    """Rend le code que l'hôte lit à l'invité : le début du SHA-256 des octets servis."""
    return hashlib.sha256(launcher).hexdigest()[:LAUNCHER_CODE_CHARS]


def build_launcher(
    address: str,
    port: int,
    password: str,
    mod: str | os.PathLike[str] | tuple[str, ...] | LauncherRefused,
) -> bytes:
    """Rend les octets du `.bat`, ou lève `LauncherRefused`. Rien n'est écrit (R6).

    `mod` est le dossier du mod, ou ce que `read_mod` en a rendu, refus compris : sous cette
    forme, rien n'est lu ni haché ici et l'appelant peut tenir un verrou. Un refus du mod ne
    passe qu'après ceux de l'adresse, du port et du mot de passe.

    Les mêmes entrées rendent les mêmes octets : ni date, ni tirage, ni chemin de l'hôte.
    """
    _check_join(address, port, password)
    if isinstance(mod, LauncherRefused):
        raise mod
    lines = [
        *BATCH.splitlines(),
        SCRIPT_MARK,
        *POWERSHELL.splitlines(),
        DATA_MARK,
        f"{JOIN_MARK} {address} {port} {password}",
        *(mod if isinstance(mod, tuple) else read_mod(mod)),
    ]
    launcher = (CRLF.join(lines) + CRLF).encode("ascii")
    if max(map(len, lines)) > MAX_LINE_BYTES or len(launcher) >= MAX_LAUNCHER_BYTES:
        raise LauncherRefused(FORMAT_REFUSED)
    return launcher


def _check_join(address: object, port: object, password: object) -> None:
    if not is_usable_address(address):
        raise LauncherRefused(ADDRESS_REFUSED)
    if not _is_port(port):
        raise LauncherRefused(PORT_REFUSED)
    if not isinstance(password, str) or not PASSWORD_ALLOWED.fullmatch(password):
        raise LauncherRefused(PASSWORD_REFUSED)
