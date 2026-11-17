-- DuelHud : affiche les annonces du duel en texte flottant au-dessus du personnage (P9F22).
--
-- Le client n'affiche que ce qu'on lui envoie, et rien d'autre. Le service décide de chaque
-- annonce, le serveur (`DuelCore.lua`) la relaie par `sendServerCommand`, et ce fichier la pose
-- à l'écran, puis dit au serveur si elle y est.
--
-- Hors duel, il est inerte : au chargement, il s'inscrit à `OnServerCommand` et s'arrête là.
-- Aucun autre appel n'a lieu tant qu'un message `duelcore` n'est pas arrivé, et un serveur qui
-- n'est pas un serveur de duel n'en envoie jamais. Un test statique tient cette règle.
--
-- Constats dans le jar du jeu (`zombie.characters.HaloTextHelper`, relu sur un build 42.22 ;
-- le Lua du jeu appelle la forme à séparateur `[br/]`) :
--   addText(player, text)                         gris, séparateur par défaut
--   addText(player, text, separator)              le 3e argument est un séparateur, pas une
--                                                 couleur
--   addText(player, text, separator, r, g, b)     couleur en entiers de 0 à 255 : celle-ci
--   addText(player, text, separator, ColorRGB)    même chose, par objet
-- Les textes d'un même joueur se suivent dans une file, joints par le séparateur ; un texte
-- déjà à l'écran ou déjà en file n'y est pas remis.
--
-- Tout appel au jeu est sous `pcall`. Si `HaloTextHelper` manque, ou si l'appel échoue, rien ne
-- casse et rien n'est prétendu : la réponse dit `shown = false`, le serveur rend `no_hud_api`,
-- et le service repasse à `servermsg`.
--
-- P9F26 : le même canal porte l'état de tenue du joueur local (`hold`, `held` vrai ou faux).
-- Le déplacement d'un joueur se calcule sur son propre client : c'est donc ici que le verrou
-- doit tenir. `IsoPlayer.updateInternal2` rend la main dès `getIgnoreMovement()`, avant toute
-- lecture des touches ; tant que la tenue dure, chaque `OnPlayerUpdate` repose
-- `setIgnoreMovement(true)` et `stopMoving()` (une action lancée s'arrête). À la libération :
-- `setIgnoreMovement(false)`, et rien d'autre. Le client ne décide jamais : il obéit à l'état
-- reçu, et se rabat sur « libre » sans nouvelle depuis `HOLD_STALE_MS` — une session coupée
-- ne laisse pas un joueur paralysé. `OnPlayerUpdate` n'est écouté qu'au premier `hold` reçu :
-- hors duel, le fichier reste inerte.

local MODULE = "duelcore"
local ANNOUNCE = "announce"
local ANNOUNCE_RESULT = "announce_result"
-- Balise de saut de ligne du texte riche du jeu : une annonce par ligne.
local SEPARATOR = "[br/]"

-- Couleur par nature d'annonce : départ, vague reçue, lot reçu, repère de temps, fin.
local COLORS = {
    wait = { 200, 200, 200 },
    start = { 137, 232, 148 },
    wave = { 255, 105, 97 },
    supply = { 120, 200, 255 },
    mark = { 255, 215, 90 },
    ["end"] = { 255, 255, 255 },
}
local DEFAULT_COLOR = { 255, 255, 255 }

local HOLD = "hold"
-- Le serveur redit la tenue toutes les cinq secondes : trente sans nouvelle, la session est
-- tombée.
local HOLD_STALE_MS = 30000
-- État de tenue reçu : `held`, l'heure de la dernière nouvelle, et `listening` une fois
-- `OnPlayerUpdate` écouté.
local hold = { held = false, heardAt = nil, listening = false }

-- Rend vrai si le jeu a pris le texte.
local function show(player, text, tone)
    local color = COLORS[tone] or DEFAULT_COLOR
    local ok = pcall(function()
        HaloTextHelper.addText(player, text, SEPARATOR, color[1], color[2], color[3])
    end)
    return ok
end

local function reply(player, seq, shown)
    pcall(function()
        sendClientCommand(player, MODULE, ANNOUNCE_RESULT, { seq = seq, shown = shown })
    end)
end

-- Heure du client en millisecondes ; nil si le jeu ne sait pas la dire.
local function clockMs()
    local ok, now = pcall(getTimestampMs)
    if ok and type(now) == "number" then return now end
    return nil
end

local function lockMovement(player)
    pcall(function() player:setIgnoreMovement(true) end)
    pcall(function() player:stopMoving() end)
end

local function freeMovement(player)
    pcall(function() player:setIgnoreMovement(false) end)
end

-- Libère le joueur local une fois, s'il était tenu.
local function release(player)
    if not hold.held then return end
    hold.held = false
    hold.heardAt = nil
    if player then freeMovement(player) end
end

-- Tenue encore fraîche : sans heure lisible, on ne sait pas, et l'on se rabat sur « libre ».
local function stillHeld()
    if not hold.held then return false end
    local now = clockMs()
    return now ~= nil and hold.heardAt ~= nil and now - hold.heardAt <= HOLD_STALE_MS
end

local function onPlayerUpdate(player)
    if not hold.held then return end
    if not stillHeld() then
        release(player)
        return
    end
    lockMovement(player)
end

local function onHold(args)
    local found, player = pcall(getPlayer)
    if not found then player = nil end
    if args.held ~= true then
        release(player)
        return
    end
    hold.held = true
    hold.heardAt = clockMs()
    if not hold.listening then
        hold.listening = pcall(function()
            Events.OnPlayerUpdate.Add(function(updated)
                pcall(onPlayerUpdate, updated)
            end)
        end)
    end
    if player then lockMovement(player) end
end

local function onServerCommand(moduleName, command, args)
    if moduleName ~= MODULE then return end
    if command == HOLD and type(args) == "table" then return onHold(args) end
    if command ~= ANNOUNCE then return end
    if type(args) ~= "table" or type(args.text) ~= "string" then return end
    local found, player = pcall(getPlayer)
    if not found or not player then return end
    local shown = HaloTextHelper ~= nil and show(player, args.text, args.tone)
    reply(player, args.seq, shown == true)
end

Events.OnServerCommand.Add(function(moduleName, command, args)
    pcall(onServerCommand, moduleName, command, args)
end)
