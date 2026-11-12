-- DuelCore : capte les faits de jeu, publie ses capacités, applique les commandes.
--
-- Le mod ne décide rien. Il échange avec le service par fichiers dans ~/Zomboid/Lua
-- (contrat v1) ; les quantités et le contenu arrivent par les commandes. Les délais
-- ci-dessous sont ceux du contrat, pas des réglages : le mod ne lit pas runtime.json.
--
-- Constats dans les classes du jeu 42.21, qui fixent la capture :
--   OnZombieDead(zombie)                         serveur
--   OnHitZombie(zombie, wielder, part, weapon)   serveur
--   OnWeaponHitCharacter(wielder, target, weapon, damage)
--                                                coup d'arme d'un joueur, jamais une morsure :
--                                                sonde seulement
--   OnPlayerGetDamage(character, type, damage)   BodyDamage.Update, sans garde serveur : une
--                                                morsure s'y lit par BLEEDING, à chaque
--                                                mise à jour tant que la plaie saigne. Il
--                                                confirme `checkHealth`, qui le précède
--   OnBeingHitByZombie                           déclaré par LuaEventManager, tiré par aucune
--                                                classe ni aucun Lua du jeu : ne prouve rien
--   OnThrowableExplode(trap, square)             l'auteur vient de trap:getAttacker()
--   OnProcessAction("build", player, args)       serveur, porte l'auteur
--   OnCharacterDeath(character)                  serveur
--   OnObjectAdded(object)                        sans auteur ; tiré sur le serveur par du
--                                                Lua de dépôt d'objet, jamais par la
--                                                construction : sonde seulement
--   OnMechanicActionDone(character, success)     tiré par le seul client : sonde seulement,
--                                                la réparation se lit à la fin de l'action
--   OnPlayerDeath(player)                        IsoPlayer.OnDeath sort avant sur le
--                                                serveur : gardé par prudence

-- Le dossier server est aussi chargé par les clients.
if not isServer() then return end

local SIDE_BY_SERVER = { duelA = "A", duelB = "B" }
local side = SIDE_BY_SERVER[getServerName()]
if not side then return end

local PROTOCOL = 1
local MOD_VERSION = "0.1.0"
local OUT_FILE = "duel_" .. side .. "_out.txt"
local IN_FILE = "duel_" .. side .. "_in.txt"
local ACK_FILE = "duel_" .. side .. "_ack.txt"
local SESSION_FILE = "duel_" .. side .. "_session.txt"

local MS_PER_S = 1000
local FLUSH_MS = 1000
local READ_MS = 1000
local ACK_MS = 5000
local BEAT_MS = 10000

-- Garde-fou de sécurité, pas un équilibrage : aucun effet sous ce rayon du joueur.
local MIN_DISTANCE = 10
local PLACE_ATTEMPTS = 32
-- Borne haute de `spacing`, la même que celle de l'arbitre (`service/rules.py`).
local MAX_SPACING = 20
local RAIN_INTENSITY = 1.0
local SPRINT_WALK_TYPE = "sprint1"
local DUEL_MARK = "duelOrigin"

-- Au-delà, un `seq` ne vient pas du compteur du service : l'accepter bloquerait les suivants.
local MAX_CMD_SEQ = 2147483647
-- Au-delà, un coup ne désigne plus l'auteur d'une mort : le zombie a pu disparaître, ou le
-- jeu l'a recyclé. Un kill tardif, par le feu, part sans auteur plutôt que sous un faux.
local ATTACKER_TTL_MS = 10000
local ATTACKER_PURGE_MS = 30000
-- Une plaie saigne à chaque mise à jour : elle reste un seul atome tant qu'elle ne se tait pas
-- pendant ce délai. Sans quoi une morsure inonderait `out` et viderait les budgets.
local WOUND_QUIET_MS = 5000
-- Les types d'`OnPlayerGetDamage` qui disent une blessure. La faim, la soif, la maladie ou
-- une chute usent aussi la santé, sans que personne n'ait frappé.
local WOUND_TYPES = { BLEEDING = true, WEAPONHIT = true }
-- Chute de santé (échelle de 0 à 100) entre deux relevés (`READ_MS`) qui vaut blessure : le signal
-- que le serveur lit lui-même. L'usure de la faim ou de la soif reste en dessous.
local HEALTH_DROP_MIN = 1
-- Ce qui peut décider d'une fin de match, par la mort ou par le score : ces lignes n'attendent
-- pas `FLUSH_MS`. Le service conclut sur `out`, et les phases de vidage de A et B sont libres.
local FLUSH_AT_ONCE = {
    player_death = true,
    zombie_kill = true,
    damage_taken = true,
    build = true,
    vehicle_repair = true,
    explosion = true,
}

-- État de session. `session` reste nil tant que le plancher est inconnu : sans lui,
-- aucune commande n'est traitée et rien n'est émis, faute de `boot`.
local session = nil
local lastCmdSeq = 0
local pending = {}

local outSeq = 0
local outLines = {}
local ackedSeq = nil
local nextFlush = 0
local nextRead = 0
local nextAck = 0
local nextBeat = 0
local nextAttackerPurge = 0
local rainStopAt = nil
local lastNow = 0

local hooks = {}
local seenHooks = {}
local lastAttacker = {}
local lastWound = {}
local lastHealth = {}
local recentDead = {}
local deadPlayers = {}
local repairClasses = 0
local lastError = nil

local function logError(err)
    local text = tostring(err)
    if text ~= lastError then
        lastError = text
        print("DuelCore : " .. text)
    end
end

-- JSON, écriture ------------------------------------------------------------------

local NULL = {}
local ARRAY = {}

local function jsonArray(list)
    return { [ARRAY] = list }
end

local ESCAPES = {
    ['"'] = '\\"', ["\\"] = "\\\\", ["\n"] = "\\n", ["\r"] = "\\r", ["\t"] = "\\t",
}

for code = 0, 31 do
    local char = string.char(code)
    ESCAPES[char] = ESCAPES[char] or string.format("\\u%04x", code)
end

local function encodeString(text)
    local parts = {}
    for i = 1, #text do
        local char = string.sub(text, i, i)
        parts[#parts + 1] = ESCAPES[char] or char
    end
    return '"' .. table.concat(parts) .. '"'
end

local function encodeNumber(value)
    if value ~= value or value == math.huge or value == -math.huge then
        return "null"
    end
    if math.floor(value) == value then
        return string.format("%.0f", value)
    end
    return string.format("%.4f", value)
end

local encodeValue

local function encodeList(list)
    local parts = {}
    for i = 1, #list do
        parts[i] = encodeValue(list[i])
    end
    return "[" .. table.concat(parts, ",") .. "]"
end

encodeValue = function(value)
    local kind = type(value)
    if value == NULL or value == nil then return "null" end
    if kind == "string" then return encodeString(value) end
    if kind == "number" then return encodeNumber(value) end
    if kind == "boolean" then return value and "true" or "false" end
    if value[ARRAY] then return encodeList(value[ARRAY]) end
    local parts = {}
    for key, item in pairs(value) do
        parts[#parts + 1] = encodeString(key) .. ":" .. encodeValue(item)
    end
    return "{" .. table.concat(parts, ",") .. "}"
end

-- JSON, lecture : le seul format des commandes et de la session --------------------

local decodeValue

local function skipSpace(text, index)
    while true do
        local char = string.sub(text, index, index)
        if char ~= " " and char ~= "\t" and char ~= "\r" and char ~= "\n" then
            return index
        end
        index = index + 1
    end
end

local UNESCAPES = {
    ['"'] = '"', ["\\"] = "\\", ["/"] = "/", b = "\b", f = "\f", n = "\n", r = "\r", t = "\t",
}

local function decodeString(text, index)
    local parts = {}
    index = index + 1
    while true do
        local char = string.sub(text, index, index)
        if char == "" then error("chaîne non terminée") end
        if char == '"' then return table.concat(parts), index + 1 end
        if char == "\\" then
            local code = string.sub(text, index + 1, index + 1)
            if code == "u" then
                local hex = string.sub(text, index + 2, index + 5)
                if not string.match(hex, "^%x%x%x%x$") then error("échappement invalide") end
                parts[#parts + 1] = string.char(tonumber(hex, 16))
                index = index + 6
            else
                local plain = UNESCAPES[code]
                if not plain then error("échappement invalide") end
                parts[#parts + 1] = plain
                index = index + 2
            end
        else
            parts[#parts + 1] = char
            index = index + 1
        end
    end
end

-- La grammaire JSON, morceau par morceau : les motifs Lua n'ont pas de groupe optionnel.
-- `tonumber` seul accepterait 0100 ou 1. ; un `.` ou un `e` sans chiffres reste derrière
-- le jeton, et l'appelant le rejette.
local function decodeNumber(text, index)
    local stop = index
    if string.sub(text, stop, stop) == "-" then stop = stop + 1 end
    local whole = string.match(text, "^%d+", stop)
    if not whole or (#whole > 1 and string.sub(whole, 1, 1) == "0") then
        error("nombre invalide")
    end
    stop = stop + #whole
    stop = stop + #(string.match(text, "^%.%d+", stop) or "")
    stop = stop + #(string.match(text, "^[eE][+-]?%d+", stop) or "")
    local value = tonumber(string.sub(text, index, stop - 1))
    if not value then error("nombre invalide") end
    return value, stop
end

local function decodeArray(text, index)
    local list = {}
    index = skipSpace(text, index + 1)
    if string.sub(text, index, index) == "]" then return list, index + 1 end
    while true do
        local value
        value, index = decodeValue(text, index)
        list[#list + 1] = value
        index = skipSpace(text, index)
        local char = string.sub(text, index, index)
        if char == "]" then return list, index + 1 end
        if char ~= "," then error("tableau invalide") end
        index = index + 1
    end
end

local function decodeObject(text, index)
    local object = {}
    index = skipSpace(text, index + 1)
    if string.sub(text, index, index) == "}" then return object, index + 1 end
    while true do
        index = skipSpace(text, index)
        if string.sub(text, index, index) ~= '"' then error("clé invalide") end
        local key, value
        key, index = decodeString(text, index)
        index = skipSpace(text, index)
        if string.sub(text, index, index) ~= ":" then error("objet invalide") end
        value, index = decodeValue(text, index + 1)
        object[key] = value
        index = skipSpace(text, index)
        local char = string.sub(text, index, index)
        if char == "}" then return object, index + 1 end
        if char ~= "," then error("objet invalide") end
        index = index + 1
    end
end

decodeValue = function(text, index)
    index = skipSpace(text, index)
    local char = string.sub(text, index, index)
    if char == "{" then return decodeObject(text, index) end
    if char == "[" then return decodeArray(text, index) end
    if char == '"' then return decodeString(text, index) end
    if string.sub(text, index, index + 3) == "true" then return true, index + 4 end
    if string.sub(text, index, index + 4) == "false" then return false, index + 5 end
    if string.sub(text, index, index + 3) == "null" then return nil, index + 4 end
    return decodeNumber(text, index)
end

-- Rend l'objet décodé, ou nil si le texte n'est pas un JSON entier.
local function decodeJson(text)
    local ok, value, index = pcall(decodeValue, text, 1)
    if not ok or skipSpace(text, index) <= #text then return nil end
    return value
end

-- `1e999` décode en infini, et l'infini passe `math.floor(value) == value`.
local function isInteger(value)
    return type(value) == "number" and value > -math.huge and value < math.huge
        and math.floor(value) == value
end

local function isCmdSeq(value, lowest)
    return isInteger(value) and value >= lowest and value <= MAX_CMD_SEQ
end

-- Émission ------------------------------------------------------------------------

local function flushOut()
    if #outLines == 0 then return end
    local writer = getFileWriter(OUT_FILE, true, true)
    if not writer then return end
    writer:write(table.concat(outLines))
    writer:close()
    outLines = {}
end

local function emit(kind, data, author)
    if not session then return false end
    outSeq = outSeq + 1
    outLines[#outLines + 1] = "{\"v\":" .. PROTOCOL
        .. ",\"boot\":" .. encodeNumber(session.boot)
        .. ",\"seq\":" .. encodeNumber(outSeq)
        .. ",\"t_ms\":" .. encodeNumber(getTimestampMs())
        .. ",\"src\":" .. encodeString(side)
        .. ",\"side\":" .. encodeValue(author)
        .. ",\"kind\":" .. encodeString(kind)
        .. ",\"data\":" .. encodeValue(data) .. "}\n"
    if FLUSH_AT_ONCE[kind] then flushOut() end
    return true
end

local function writeAck()
    local writer = getFileWriter(ACK_FILE, true, false)
    if not writer then return false end
    writer:write("{\"v\":" .. PROTOCOL .. ",\"boot\":" .. encodeNumber(session.boot)
        .. ",\"seq\":" .. encodeNumber(lastCmdSeq) .. "}\n")
    writer:close()
    return true
end

-- Joueurs -------------------------------------------------------------------------

local function isPlayer(object)
    return object ~= nil and instanceof(object, "IsoPlayer")
end

-- Le camp de l'auteur : le serveur n'héberge qu'un camp, l'auteur doit être un joueur.
local function sideOf(character)
    if isPlayer(character) then return side end
    return NULL
end

local function onlinePlayers()
    local players = getOnlinePlayers()
    local list = {}
    if players then
        for i = 0, players:size() - 1 do
            list[#list + 1] = players:get(i)
        end
    end
    return list
end

local function emitBeat()
    local names = {}
    local players = onlinePlayers()
    for i = 1, #players do
        names[i] = players[i]:getUsername()
    end
    local player = players[1]
    emit("beat", {
        age = getGameTime():getWorldAgeHours(),
        players = jsonArray(names),
        hp = player and player:getBodyDamage():getOverallBodyHealth() or NULL,
        kills = player and player:getZombieKills() or NULL,
    }, side)
end

-- Capture -------------------------------------------------------------------------

local function weaponOf(character)
    local item = isPlayer(character) and character:getPrimaryHandItem() or nil
    return item and item:getFullType() or NULL
end

local function positionData(character, data)
    if character then
        data.x = math.floor(character:getX())
        data.y = math.floor(character:getY())
    end
    return data
end

-- Le service ne compte un atome que s'il vient de l'identité du camp : le nom part avec lui.
local function authoredBy(character, data)
    if isPlayer(character) then data.player = character:getUsername() end
    return data
end

-- Sans preuve de réception, un hook reste `false` côté service, même enregistré.
local function noteHook(name)
    if not seenHooks[name] and emit("hook_seen", { hook = name }, side) then
        seenHooks[name] = true
    end
end

local function onHitZombie(zombie, wielder)
    if isPlayer(wielder) then
        lastAttacker[zombie] = {
            wielder = wielder,
            at = getTimestampMs(),
            target = zombie:getOnlineID(),
        }
    end
end

-- La table est rebâtie : une entrée n'y survit que par un coup récent.
local function purgeAttackers(now)
    local kept = {}
    for zombie, hit in pairs(lastAttacker) do
        if now - hit.at < ATTACKER_TTL_MS then kept[zombie] = hit end
    end
    lastAttacker = kept
end

local function isOnline(player)
    local players = onlinePlayers()
    for i = 1, #players do
        if players[i] == player then return true end
    end
    return false
end

-- Le jeu recycle ses objets : un coup ne vaut que récent, sur le même zombie, et d'un joueur
-- encore en jeu. `target` écarte un recyclage dans la fenêtre, si l'identifiant a changé.
local function freshAuthor(hit, zombie, now)
    if not hit or now - hit.at >= ATTACKER_TTL_MS then return nil end
    if hit.target ~= zombie:getOnlineID() then return nil end
    local wielder = hit.wielder
    if not isPlayer(wielder) or wielder:isDead() or not isOnline(wielder) then return nil end
    return wielder
end

local function onZombieDead(zombie)
    local hit = lastAttacker[zombie]
    lastAttacker[zombie] = nil

    -- Le jeu peut tirer ce hook deux fois pour un zombie qui brûle.
    if recentDead[zombie] then return end
    recentDead[zombie] = true

    local author = zombie:getAttackedBy()
    if not isPlayer(author) then author = freshAuthor(hit, zombie, getTimestampMs()) end

    -- Le jeu recycle ses zombies sans vider leur modData : la marque ne survit pas au mort.
    local modData = zombie:getModData()
    local origin = modData[DUEL_MARK] and "duel" or "world"
    modData[DUEL_MARK] = nil

    local data = authoredBy(author, { w = weaponOf(author), origin = origin })
    emit("zombie_kill", positionData(zombie, data), sideOf(author))
end

local function attackerName(attacker)
    if isPlayer(attacker) then return attacker:getUsername() end
    if attacker and instanceof(attacker, "IsoZombie") then return "zombie" end
    return NULL
end

-- La preuve est une blessure reçue, pas une réception quelconque du hook. `hook` nul : la
-- blessure vient du relevé de santé, qui ne prouve aucun hook. Les deux sources partagent
-- `lastWound` : une plaie vue des deux côtés reste un seul atome, et le hook la confirme.
local function reportWound(hook, player, attacker, damage)
    local name = player:getUsername()
    local now = getTimestampMs()
    local quiet = not lastWound[name] or now - lastWound[name] >= WOUND_QUIET_MS
    lastWound[name] = now
    if quiet then
        emit("damage_taken", positionData(player, authoredBy(player, {
            hp = player:getBodyDamage():getOverallBodyHealth(),
            attacker = attackerName(attacker),
            damage = damage or NULL,
            source = hook and "hook" or "health",
        })), side)
    end
    if hook then noteHook(hook) end
end

-- Le signal premier de `damage_taken` : il ne dépend d'aucun hook. Le premier relevé d'un joueur
-- pose son repère, et une santé qui remonte, soin ou nouveau personnage, n'est pas une blessure.
local function checkHealth(player)
    local name = player:getUsername()
    local health = player:getBodyDamage():getOverallBodyHealth()
    local before = lastHealth[name]
    lastHealth[name] = health
    if before and before - health >= HEALTH_DROP_MIN then
        reportWound(nil, player, player:getAttackedBy(), before - health)
    end
end

local function onPlayerGetDamage(character, damageType, damage)
    if isPlayer(character) and WOUND_TYPES[damageType] then
        reportWound("OnPlayerGetDamage", character, character:getAttackedBy(), damage)
    end
end

-- Jamais tiré en 42.21, donc sans signature constatée : sans cible, le joueur du serveur.
local function onBeingHitByZombie(attacker, target)
    local player = isPlayer(target) and target or onlinePlayers()[1]
    if player then reportWound("OnBeingHitByZombie", player, attacker, nil) end
end

local function onProcessAction(action, character)
    if action ~= "build" then return end
    emit("build", positionData(character, authoredBy(character, {})), sideOf(character))
end

local function onThrowableExplode(trap, square)
    local author = trap and trap:getAttacker() or nil
    local data = authoredBy(author, {})
    if square then
        data.x = square:getX()
        data.y = square:getY()
    end
    emit("explosion", data, sideOf(author))
end

local function reportDeath(player)
    local name = player:getUsername()
    if deadPlayers[name] then return end
    deadPlayers[name] = true
    -- Une mort entre deux relevés reste une blessure : la perte de santé part avant elle.
    checkHealth(player)
    emit("player_death", positionData(player, { player = name }), side)
end

local function onCharacterDeath(character)
    if isPlayer(character) then reportDeath(character) end
end

-- Filet si aucun hook de mort ne tire : l'état du joueur fait foi.
local function checkDeaths()
    local players = onlinePlayers()
    for i = 1, #players do
        local player = players[i]
        if player:isDead() then
            reportDeath(player)
        else
            deadPlayers[player:getUsername()] = nil
            checkHealth(player)
        end
    end
end

local function partState(action)
    local part = action.part or action.vehiclePart
    if not part then return nil end
    return { part = part, condition = part:getCondition(), installed = part:getInventoryItem() ~= nil }
end

local function reportRepair(action, before)
    if not before then return end
    local part = before.part
    local repaired = part:getCondition() > before.condition
    local installed = not before.installed and part:getInventoryItem() ~= nil
    if repaired or installed then
        local character = action.character
        emit("vehicle_repair", positionData(character, authoredBy(character, {})), sideOf(character))
    end
end

-- Sur le serveur, `complete` est appelée par son nom sur la classe de l'action : la
-- remplacer suffit. La capture ne doit jamais perturber l'action du joueur.
local function watchRepairAction(className)
    local class = _G[className]
    if type(class) ~= "table" or type(class.complete) ~= "function" then return end
    local complete = class.complete
    class.complete = function(self, ...)
        local ok, before = pcall(partState, self)
        local result = complete(self, ...)
        if ok then
            local reported, err = pcall(reportRepair, self, before)
            if not reported then logError(err) end
        end
        return result
    end
    repairClasses = repairClasses + 1
end

-- Sondes --------------------------------------------------------------------------

-- `selfProved` : le traitement appelle lui-même `noteHook`, sur ce qui prouve le hook.
local function register(name, handler, selfProved)
    local event = Events[name]
    hooks[name] = false
    if not event or not event.Add then return end
    local ok = pcall(event.Add, function(...)
        local done, err = pcall(function(...)
            if not selfProved then noteHook(name) end
            if handler then handler(...) end
        end, ...)
        if not done then logError(err) end
    end)
    hooks[name] = ok
end

register("OnZombieDead", onZombieDead)
register("OnHitZombie", onHitZombie)
register("OnPlayerGetDamage", onPlayerGetDamage, true)
register("OnBeingHitByZombie", onBeingHitByZombie, true)
register("OnWeaponHitCharacter", nil)
register("OnProcessAction", onProcessAction)
register("OnThrowableExplode", onThrowableExplode)
register("OnCharacterDeath", onCharacterDeath)
register("OnPlayerDeath", onCharacterDeath)
register("OnObjectAdded", nil)
register("OnMechanicActionDone", nil)
register("OnEnterVehicle", nil)

watchRepairAction("ISRepairEngine")
watchRepairAction("ISFixVehiclePartAction")
watchRepairAction("ISInstallVehiclePart")

-- Placement -----------------------------------------------------------------------

local function distanceBetween(ax, ay, bx, by)
    local dx, dy = ax - bx, ay - by
    return math.sqrt(dx * dx + dy * dy)
end

local function needNumber(cmd, key)
    local value = cmd[key]
    if type(value) ~= "number" then error("paramètre absent : " .. key) end
    return value
end

local function freeSquare(x, y)
    local square = getCell():getGridSquare(x, y, 0)
    if square and square:isFree(false) then return square end
    return nil
end

-- Une case libre à une distance du joueur comprise dans `distance`, ou nil. `accept`,
-- facultatif, écarte une case libre que l'appelant ne veut pas (trop près d'une autre).
local function pickSquare(player, distance, accept)
    local low, high = distance[1], distance[2]
    local px, py = player:getX(), player:getY()
    for _ = 1, PLACE_ATTEMPTS do
        local angle = math.rad(ZombRand(360))
        local radius = ZombRand(low, high + 1)
        local x = math.floor(px + math.cos(angle) * radius)
        local y = math.floor(py + math.sin(angle) * radius)
        local gap = distanceBetween(x, y, px, py)
        if gap >= low and gap <= high and (not accept or accept(x, y)) then
            local square = freeSquare(x, y)
            if square then return square end
        end
    end
    return nil
end

-- Une case libre à moins de `radius` du centre, jamais sous le plancher du joueur.
local function pickAround(player, center, radius)
    local px, py = player:getX(), player:getY()
    local cx, cy = center:getX(), center:getY()
    for _ = 1, PLACE_ATTEMPTS do
        local x = cx + ZombRand(-radius, radius + 1)
        local y = cy + ZombRand(-radius, radius + 1)
        if distanceBetween(x, y, cx, cy) <= radius and distanceBetween(x, y, px, py) >= MIN_DISTANCE then
            local square = freeSquare(x, y)
            if square then return square end
        end
    end
    return nil
end

-- Crée `count` zombies sur `square` et les ajoute à `created` ; rend le nombre créé.
local function spawnAt(square, count, prepare, created)
    local zombies = addZombiesInOutfit(square:getX(), square:getY(), 0, count, nil, nil)
    if not zombies then return 0 end
    local spawned = zombies:size()
    for i = 0, spawned - 1 do
        local zombie = zombies:get(i)
        zombie:getModData()[DUEL_MARK] = true
        if prepare then prepare(zombie) end
        if created then created[#created + 1] = zombie end
    end
    return spawned
end

local function spawnResult(spawned, extra)
    if spawned == 0 then
        return { status = "failed", reason = "no_spawn", spawned = 0 }
    end
    local result = { status = "applied", spawned = spawned }
    if extra then
        for key, value in pairs(extra) do result[key] = value end
    end
    return result
end

local NO_SQUARE = { status = "failed", reason = "no_square" }

-- Réglage facultatif de la commande (P9F8) : absent ou hors bornes, la valeur sûre `low`
-- ou la borne franchie. Le preset le règle, l'arbitre le valide : le mod ne fait que replier.
local function optionalNumber(cmd, key, low, high)
    local value = cmd[key]
    if type(value) ~= "number" or value ~= value or value < low then return low end
    if value > high then return high end
    return value
end

-- Cases du lot (P9F8) : la première comme avant, puis une par zombie, à au moins
-- `spacing` cases de toutes celles déjà retenues. `spacing` nul garde le tas d'avant.
local function pickSpread(player, distance, count, spacing)
    local first = pickSquare(player, distance)
    if not first then return nil end
    local squares = { first }
    if spacing <= 0 then return squares end
    local function farFromAll(x, y)
        for _, square in ipairs(squares) do
            if distanceBetween(x, y, square:getX(), square:getY()) < spacing then return false end
        end
        return true
    end
    for _ = 2, count do
        local square = pickSquare(player, distance, farFromAll)
        if not square then break end
        squares[#squares + 1] = square
    end
    return squares
end

-- Oriente un zombie vers le joueur (P9F8). Chaque appel sous `pcall` : une fonction absente
-- d'une version du jeu laisse le zombie créé, simplement non compté comme ciblé.
local function targetPlayer(zombie, player, aggro)
    local okTarget = pcall(function() zombie:setTarget(player) end)
    local okAggro = pcall(function() zombie:addAggro(player, aggro) end)
    local okSpotted = pcall(function() zombie:spotted(player, true) end)
    return okTarget and okAggro and okSpotted
end

-- Traitements par `kind` ----------------------------------------------------------

local handlers = {}

-- Le nombre ne baisse jamais : faute de cases espacées en nombre, les zombies restants
-- naissent sur les cases déjà retenues, à tour de rôle. `spread` dit combien de cases
-- distinctes ont reçu un zombie, `targeted` combien de zombies ont été orientés.
local function spawnGroup(cmd, player, prepare)
    local count = needNumber(cmd, "n")
    local spacing = math.floor(optionalNumber(cmd, "spacing", 0, MAX_SPACING))
    local aggro = optionalNumber(cmd, "aggro", 0, 1)
    local squares = pickSpread(player, cmd.distance, count, spacing)
    if not squares then return NO_SQUARE end
    local perSquare = {}
    for i = 1, count do
        local slot = (i - 1) % #squares + 1
        perSquare[slot] = (perSquare[slot] or 0) + 1
    end
    local created, spawned, spread = {}, 0, 0
    for slot, square in ipairs(squares) do
        local made = spawnAt(square, perSquare[slot], prepare, created)
        spawned = spawned + made
        if made > 0 then spread = spread + 1 end
    end
    local targeted = 0
    if aggro > 0 then
        for _, zombie in ipairs(created) do
            if targetPlayer(zombie, player, aggro) then targeted = targeted + 1 end
        end
    end
    return spawnResult(spawned, { spread = spread, targeted = targeted })
end

local function makeSprinter(zombie)
    zombie:setWalkType(SPRINT_WALK_TYPE)
    zombie:setSpeedTypeFromWalkType()
end

handlers.spawn_pop = function(cmd, player)
    return spawnGroup(cmd, player, nil)
end

handlers.probe_spawn = handlers.spawn_pop

handlers.spawn_sprinter = function(cmd, player)
    return spawnGroup(cmd, player, makeSprinter)
end

handlers.horde_area = function(cmd, player)
    local center = pickSquare(player, cmd.distance)
    if not center then return NO_SQUARE end
    local radius = needNumber(cmd, "radius")
    local spawned = 0
    for _ = 1, needNumber(cmd, "n") do
        local square = pickAround(player, center, radius)
        if square then spawned = spawned + spawnAt(square, 1, nil, nil) end
    end
    return spawnResult(spawned)
end

handlers.noise = function(cmd, player)
    local square = pickSquare(player, cmd.distance)
    if not square then return NO_SQUARE end
    local radius = needNumber(cmd, "radius")
    addSound(player, square:getX(), square:getY(), 0, radius, radius)
    return { status = "applied" }
end

handlers.weather_rain = function(cmd)
    local stopAt = getTimestampMs() + needNumber(cmd, "duration_s") * MS_PER_S
    getClimateManager():transmitServerStartRain(RAIN_INTENSITY)
    if not rainStopAt or stopAt > rainStopAt then rainStopAt = stopAt end
    return { status = "applied" }
end

handlers.supply_drop = function(cmd, player)
    -- Tout est créé avant le premier dépôt : un objet inconnu ne laisse rien au sol.
    local created = {}
    for _, entry in ipairs(cmd.items) do
        for _ = 1, entry.count do
            local ok, item = pcall(instanceItem, entry.item)
            if not ok or not item then
                return { status = "failed", reason = "unknown_item" }
            end
            created[#created + 1] = item
        end
    end
    local square = pickSquare(player, cmd.distance)
    if not square then return NO_SQUARE end
    for _, item in ipairs(created) do
        square:AddWorldInventoryItem(item, 0, 0, 0)
    end
    return { status = "applied" }
end

-- Tenue d'avant-match (P9F10, complétée en P9F11) ---------------------------------------
--
-- Tant que l'adversaire manque, le monde est forcé en pause : `hold_time` capte l'heure du
-- jour, la réapplique à chaque passe (`holdTick`), et fige les joueurs présents ou à venir.
-- `release_time` rend la main. Ce sont des commandes de monde : elles ne visent aucun joueur
-- et passent sans `refusalReason`. Tout appel au jeu est sous `pcall` : une fonction absente
-- rend `refused`, `no_hold_api`, avec le nom de l'appel (`call`), et le match continue sans
-- verrou, annoncé par le service.
--
-- Il n'existe aucune pause générale d'un serveur où quelqu'un joue : `GameTime.isGamePaused`
-- n'est vrai que serveur vide et `PauseEmpty` (bytecode). La tenue se construit donc avec des
-- leviers, tous exigés :
--   * le pas de temps (`GameTime:setMultiplier`, levier de `setTimeSpeedMultiplier`), lu par
--     zombies, besoins et horloge à chaque passe : posé à `HOLD_MULTIPLIER`, rendu au relâchement.
--     Pas zéro : `getInvMultiplier` divise par lui ;
--   * l'heure du jour, réappliquée à chaque passe ;
--   * le joueur : invulnérable (`setInvulnerable`, P9F11 : aucun coup ne lui ôte de vie), hors
--     d'atteinte des zombies (`setZombiesDontAttack`), puis figé (`setBlockMovement`). Protégé
--     d'abord, figé ensuite ; au relâchement, rendu mobile d'abord, vulnérable ensuite : si la
--     mobilité ne revient pas, la protection reste. Jamais un joueur figé et exposé.
-- Best-effort en plus : ses besoins (faim, soif, fatigue, endurance) sont reposés à chaque
-- passe à leur valeur d'entrée en tenue (`Stats:set` : `CharacterStat` peut manquer à Lua).
local function holdRefused(call)
    return { status = "refused", reason = "no_hold_api", call = call }
end
-- Pas de temps du monde sous tenue : dix mille fois plus lent, trois minutes y durent 18 ms.
local HOLD_MULTIPLIER = 0.0001
-- Pas d'avant la tenue, rendu au relâchement ; nil hors tenue.
local heldMultiplier = nil
-- Vrai dès que la tenue a réellement posé `HOLD_MULTIPLIER` : seul ce cas exige de rendre le
-- pas. Un `setMultiplier` absent ne change rien, et ne doit pas bloquer le relâchement.
local multiplierChanged = false
-- Heure du jour figée, nil hors tenue.
local heldTime = nil
-- `seq` de la dernière `hold_time` appliquée : un échec ultérieur s'y rapporte, une fois.
local heldSeq = nil
-- Par joueur tenu (nom) : le joueur, ce que la tenue lui a posé, sa protection d'avant, et
-- ses besoins captés à l'entrée. Une entrée ne part qu'une fois le joueur entièrement rendu.
local heldPlayers = {}
local FROZEN_STATS = { "HUNGER", "THIRST", "FATIGUE", "ENDURANCE" }
-- Déclarée ici, définie avec les commandes : un échec différé de la tenue s'y annonce.
local emitResult

local function playerKey(player)
    local ok, name = pcall(function() return player:getUsername() end)
    if ok and name ~= nil then return tostring(name) end
    return tostring(player)
end

local function captureStats(player)
    local stats = {}
    pcall(function()
        local holder = player:getStats()
        for _, name in ipairs(FROZEN_STATS) do
            local stat = CharacterStat[name]
            stats[#stats + 1] = { stat = stat, value = holder:get(stat) }
        end
    end)
    return stats
end

local function restoreStats(player, stats)
    pcall(function()
        local holder = player:getStats()
        for _, entry in ipairs(stats) do holder:set(entry.stat, entry.value) end
    end)
end

-- Les appels du jeu, chacun nommé : son absence s'annonce sous ce nom. Écrits en `:` explicite,
-- comme partout dans le mod, plutôt que par indexation dynamique de l'objet Java.
local PLAYER_SETTERS = {
    setInvulnerable = function(player, value) player:setInvulnerable(value) end,
    setZombiesDontAttack = function(player, value) player:setZombiesDontAttack(value) end,
    setBlockMovement = function(player, value) player:setBlockMovement(value) end,
}
local PLAYER_GETTERS = {
    isInvulnerable = function(player) return player:isInvulnerable() end,
    isZombiesDontAttack = function(player) return player:isZombiesDontAttack() end,
}

local function readFlag(player, getter)
    local ok, value = pcall(PLAYER_GETTERS[getter], player)
    return ok and value == true
end

local function callPlayer(player, setter, value)
    return pcall(PLAYER_SETTERS[setter], player, value)
end

-- Protège puis fige un joueur, à chaque passe : un joueur arrivé après la commande l'est à sa
-- première passe. Rend faux et le nom de l'appel manquant ; ce qui a été posé est noté dans
-- l'entrée, pour être rendu tel quel.
local function freezePlayer(player)
    local key = playerKey(player)
    local entry = heldPlayers[key]
    if not entry then
        entry = {
            player = player,
            invulnerable = readFlag(player, "isInvulnerable"),
            shielded = readFlag(player, "isZombiesDontAttack"),
            stats = captureStats(player),
            set = {},
        }
        heldPlayers[key] = entry
    end
    entry.player = player
    -- Protégé d'abord : un joueur n'est jamais figé sans être hors d'atteinte.
    if not callPlayer(player, "setInvulnerable", true) then return false, "setInvulnerable" end
    entry.set.setInvulnerable = true
    if not callPlayer(player, "setZombiesDontAttack", true) then
        return false, "setZombiesDontAttack"
    end
    entry.set.setZombiesDontAttack = true
    if not callPlayer(player, "setBlockMovement", true) then return false, "setBlockMovement" end
    entry.set.setBlockMovement = true
    restoreStats(player, entry.stats)
    return true
end

-- Rend un joueur dans l'ordre inverse : mobile d'abord, vulnérable ensuite. Si la mobilité ne
-- revient pas, la protection reste et l'entrée aussi : un prochain `release_time` réessaie.
local function thawPlayer(key, entry)
    local player = entry.player
    if entry.set.setBlockMovement then
        if not callPlayer(player, "setBlockMovement", false) then return false, "setBlockMovement" end
        entry.set.setBlockMovement = nil
    end
    if entry.set.setZombiesDontAttack then
        if not callPlayer(player, "setZombiesDontAttack", entry.shielded) then
            return false, "setZombiesDontAttack"
        end
        entry.set.setZombiesDontAttack = nil
    end
    if entry.set.setInvulnerable then
        if not callPlayer(player, "setInvulnerable", entry.invulnerable) then
            return false, "setInvulnerable"
        end
        entry.set.setInvulnerable = nil
    end
    heldPlayers[key] = nil
    return true
end

-- Fige les joueurs en ligne, ou rend tous ceux que la tenue a touchés, en ligne ou non. Rend
-- faux et le premier appel manquant si un seul joueur n'a pu être figé ou rendu.
local function blockPlayers(blocked)
    local missing = nil
    if blocked then
        local players = onlinePlayers()
        for i = 1, #players do
            local ok, call = freezePlayer(players[i])
            if not ok and missing == nil then missing = call end
        end
    else
        local keys = {}
        for key in pairs(heldPlayers) do keys[#keys + 1] = key end
        for _, key in ipairs(keys) do
            local ok, call = thawPlayer(key, heldPlayers[key])
            if not ok and missing == nil then missing = call end
        end
    end
    return missing == nil, missing
end

-- Best-effort : l'écran de pause dit au joueur qu'on l'attend ; son échec ne lève pas le gel.
local function showPaused(shown)
    pcall(function() setShowPausedMessage(shown) end)
end

local function readTimeOfDay()
    local ok, time = pcall(function() return getGameTime():getTimeOfDay() end)
    if ok and type(time) == "number" then return time end
    return nil
end

local function writeTimeOfDay(time)
    return pcall(function() getGameTime():setTimeOfDay(time) end)
end

-- Pas d'avant la tenue. Un pas déjà au ralenti (tenue sauvée par un serveur tombé) n'est pas
-- une valeur d'avant : on rend alors le pas normal, 1.
local function readMultiplier()
    local ok, value = pcall(function() return getGameTime():getTrueMultiplier() end)
    if ok and type(value) == "number" and value > HOLD_MULTIPLIER then return value end
    return 1
end

local function writeMultiplier(value)
    return pcall(function() getGameTime():setMultiplier(value) end)
end

-- Lève toute la tenue : plus d'heure réappliquée, pas de temps rendu, joueurs rendus, écran
-- de pause retiré. Rend faux et l'appel manquant si le pas ou un joueur n'a pu être rendu.
local function dropHold()
    heldTime = nil
    local missing = nil
    if heldMultiplier ~= nil then
        if not multiplierChanged then
            heldMultiplier = nil
        elseif writeMultiplier(heldMultiplier) then
            heldMultiplier = nil
            multiplierChanged = false
        else
            missing = "setMultiplier"
        end
    end
    showPaused(false)
    local thawed, call = blockPlayers(false)
    if not thawed and missing == nil then missing = call end
    return missing == nil, missing
end

-- Tous les leviers de la passe ; rend faux et le premier appel manquant.
local function applyHold(time)
    if not writeTimeOfDay(time) then return false, "setTimeOfDay" end
    if not writeMultiplier(HOLD_MULTIPLIER) then return false, "setMultiplier" end
    multiplierChanged = true
    return blockPlayers(true)
end

-- Réappliquée à chaque passe, même monde en pause : un joueur arrivé pendant la tenue est
-- protégé et figé dès sa première passe, et l'heure ne garde jamais plus d'une passe d'avance.
-- Un joueur qu'on ne peut tenir (API absente à son arrivée) fait tomber la tenue entière,
-- rendue une seule fois `refused`, `no_hold_api` et l'appel, sous le `seq` de `hold_time` :
-- le service annonce un départ non figé.
local function holdTick()
    if heldTime == nil then return end
    local ok, call = applyHold(heldTime)
    if ok then return end
    local seq = heldSeq
    heldSeq = nil
    dropHold()
    if seq ~= nil then
        emitResult(seq, holdRefused(call))
        flushOut()
    end
end

-- Idempotente : renvoyée sur une tenue déjà active, elle garde l'heure captée la première fois.
handlers.hold_time = function(cmd)
    local time = heldTime or readTimeOfDay()
    if time == nil then return holdRefused("getTimeOfDay") end
    if heldMultiplier == nil then heldMultiplier = readMultiplier() end
    local ok, call = applyHold(time)
    if not ok then
        heldSeq = nil
        dropHold()
        return holdRefused(call)
    end
    heldTime = time
    heldSeq = cmd.seq
    showPaused(true)
    return { status = "applied" }
end

-- Rien de tenu : `applied`. Un joueur qu'on ne peut rendre mobile garde sa protection, et le
-- relâchement rend `refused` : le service le relance, et ne lance pas le duel sans lui.
handlers.release_time = function()
    heldSeq = nil
    local ok, call = dropHold()
    if not ok then return holdRefused(call) end
    return { status = "applied" }
end

-- Commandes qui visent le monde, pas un joueur : elles s'appliquent serveur vide.
local WORLD_COMMANDS = { hold_time = true, release_time = true }

-- Les capacités publiées : un effet n'existe que si sa fonction de jeu existe.
local EFFECT_FUNCTIONS = {
    spawn_pop = "addZombiesInOutfit",
    spawn_sprinter = "addZombiesInOutfit",
    horde_area = "addZombiesInOutfit",
    probe_spawn = "addZombiesInOutfit",
    noise = "addSound",
    weather_rain = "getClimateManager",
    supply_drop = "instanceItem",
}

local function emitCaps()
    local functions = {}
    local effects = {}
    for effect, name in pairs(EFFECT_FUNCTIONS) do
        functions[name] = _G[name] ~= nil
        if functions[name] then effects[#effects + 1] = effect end
    end

    local atoms = {}
    local function addAtom(atom, available)
        if available then atoms[#atoms + 1] = atom end
    end
    addAtom("zombie_kill", hooks.OnZombieDead)
    -- `checkHealth` tourne dans `OnTick` : l'atome ne dépend pas de `OnPlayerGetDamage`.
    addAtom("damage_taken", hooks.OnTick)
    addAtom("build", hooks.OnProcessAction)
    addAtom("vehicle_repair", repairClasses > 0)
    addAtom("explosion", hooks.OnThrowableExplode)

    emit("caps", {
        build = getCore():getGameAndBuildVersion(),
        mod = MOD_VERSION,
        protocol = PROTOCOL,
        -- Le mod comprend `hold_time` et `release_time` (P9F10) ; le jeu, lui, peut encore les
        -- refuser par `no_hold_api`.
        hold = true,
        hooks = hooks,
        functions = functions,
        atoms = jsonArray(atoms),
        effects = jsonArray(effects),
    }, side)
end

-- Commandes -----------------------------------------------------------------------

-- Seule fonction à ouvrir `in`. Une ligne n'existe que par son `\n` : le reste en fin
-- de fichier n'est pas rendu, même si son JSON est entier, et sera relu au passage
-- suivant. Lire un caractère à la fois rend ce `\n` observable.
local function readCommandLines()
    local lines = {}
    local reader = getFileReader(IN_FILE, false)
    if not reader then return lines end
    local ok, err = pcall(function()
        local chars = {}
        while true do
            local code = reader:read()
            if code < 0 then return end
            if code == 10 then
                lines[#lines + 1] = table.concat(chars)
                chars = {}
            else
                chars[#chars + 1] = string.char(code)
            end
        end
    end)
    reader:close()
    if not ok then logError(err) end
    return lines
end

-- `lastCmdSeq` avance à la prise en charge, avant l'attente et avant le pcall : `in`
-- est relu toutes les secondes, et une avance à l'application rejouerait la commande.
local function acceptCommandLine(line)
    local cmd = decodeJson(line)
    if type(cmd) ~= "table" or cmd.v ~= PROTOCOL then return end
    if not isCmdSeq(cmd.seq, 1) then
        logError("commande refusée, seq invalide : " .. tostring(cmd.seq))
        return
    end
    if type(cmd.kind) ~= "string" then return end
    if cmd.seq <= lastCmdSeq or cmd.seq <= session.cmd_floor then return end
    lastCmdSeq = cmd.seq
    local warn = type(cmd.warn_s) == "number" and cmd.warn_s or 0
    pending[#pending + 1] = { cmd = cmd, due = getTimestampMs() + warn * MS_PER_S }
end

local function refusalReason(player, cmd)
    if not player then return "no_player" end
    if player:isAsleep() then return "asleep" end
    if player:getVehicle() then return "in_vehicle" end
    if cmd.distance and cmd.distance[1] < MIN_DISTANCE then return "too_close" end
    return nil
end

local function applyCommand(cmd)
    local handler = handlers[cmd.kind]
    if not handler then
        return { status = "failed", reason = "unknown_kind" }
    end
    if WORLD_COMMANDS[cmd.kind] then return handler(cmd) end
    local player = onlinePlayers()[1]
    local reason = refusalReason(player, cmd)
    if reason then
        return { status = "refused", reason = reason }
    end
    return handler(cmd, player)
end

emitResult = function(seq, result)
    emit("cmd_result", {
        seq = seq,
        status = result.status,
        reason = result.reason,
        spawned = result.spawned,
        spread = result.spread,
        targeted = result.targeted,
        -- P9F11 : l'appel du jeu qui manque, pour `no_hold_api`.
        call = result.call,
    }, side)
end

-- La commande quitte `pending` avant d'être appliquée : un seul résultat par `seq`.
local function applyDueCommands(now)
    local index = 1
    while index <= #pending do
        local entry = pending[index]
        if entry.due <= now then
            table.remove(pending, index)
            local ok, result = pcall(applyCommand, entry.cmd)
            if ok then
                emitResult(entry.cmd.seq, result)
            else
                logError(result)
                emitResult(entry.cmd.seq, { status = "failed", reason = "lua_error" })
            end
            -- Le service attend ce résultat au lobby pour annoncer un départ non figé.
            if WORLD_COMMANDS[entry.cmd.kind] then flushOut() end
        else
            index = index + 1
        end
    end
end

local function stopRainIfDue(now)
    if rainStopAt and now >= rainStopAt then
        rainStopAt = nil
        getClimateManager():transmitServerStopRain()
    end
end

-- Boucle --------------------------------------------------------------------------

local function readSessionText()
    local reader = getFileReader(SESSION_FILE, false)
    if not reader then return nil end
    local chars = {}
    local ok, err = pcall(function()
        while true do
            local code = reader:read()
            if code < 0 then return end
            chars[#chars + 1] = string.char(code)
        end
    end)
    reader:close()
    if not ok then
        logError(err)
        return nil
    end
    return table.concat(chars)
end

local function openSession(now)
    local text = readSessionText()
    local loaded = text and decodeJson(text)
    if type(loaded) ~= "table" or loaded.v ~= PROTOCOL or loaded.side ~= side then return end
    if not isInteger(loaded.boot) then return end
    if not isCmdSeq(loaded.cmd_floor, 0) then
        logError("session refusée, cmd_floor invalide : " .. tostring(loaded.cmd_floor))
        return
    end
    session = loaded
    lastCmdSeq = session.cmd_floor
    nextBeat = now + BEAT_MS
    emitCaps()
end

-- L'heure murale peut reculer : ce qui est daté d'elle recule d'autant, et chaque délai garde
-- ce qu'il lui restait. Sinon les heartbeats se tairaient le temps du recul, et le service,
-- sur son horloge monotone, y verrait une absence.
local function absorbClockStep(now)
    local back = lastNow - now
    lastNow = now
    if back <= 0 then return end
    nextFlush = nextFlush - back
    nextRead = nextRead - back
    nextAck = nextAck - back
    nextBeat = nextBeat - back
    nextAttackerPurge = nextAttackerPurge - back
    if rainStopAt then rainStopAt = rainStopAt - back end
    for i = 1, #pending do
        pending[i].due = pending[i].due - back
    end
    for _, hit in pairs(lastAttacker) do
        hit.at = hit.at - back
    end
    for name, at in pairs(lastWound) do
        lastWound[name] = at - back
    end
end

local function step()
    -- getTimestampMs et non EveryOneMinute : le cadencement suit le temps réel.
    local now = getTimestampMs()
    absorbClockStep(now)
    if now >= nextAttackerPurge then
        nextAttackerPurge = now + ATTACKER_PURGE_MS
        purgeAttackers(now)
    end

    if not session then
        if now < nextRead then return end
        nextRead = now + READ_MS
        openSession(now)
        if not session then return end
    end

    if now >= nextRead then
        nextRead = now + READ_MS
        local lines = readCommandLines()
        for i = 1, #lines do
            acceptCommandLine(lines[i])
        end
        checkDeaths()
    end

    applyDueCommands(now)
    stopRainIfDue(now)

    if now >= nextBeat then
        nextBeat = now + BEAT_MS
        emitBeat()
    end

    if lastCmdSeq ~= ackedSeq or now >= nextAck then
        if writeAck() then
            ackedSeq = lastCmdSeq
            nextAck = now + ACK_MS
        end
    end

    if now >= nextFlush then
        nextFlush = now + FLUSH_MS
        recentDead = {}
        flushOut()
    end
end

local function runStep()
    local ok, err = pcall(step)
    if not ok then logError(err) end
    ok, err = pcall(holdTick)
    if not ok then logError(err) end
end

-- Point d'entrée hors tick (P9F10) : avec `PauseEmpty=true`, un serveur vide ne tique pas,
-- et `caps` doit pourtant partir pour que le contrat aboutisse. `OnServerStarted` tire une
-- fois, après le chargement du monde, dans `GameServer.startServer` : la session y est lue et
-- `caps` vidé dans `out` sur-le-champ. Le tiqueur reste la voie de secours.
local function startSession()
    if session then return end
    openSession(getTimestampMs())
    if session then flushOut() end
end

if Events.OnServerStarted and Events.OnServerStarted.Add then
    pcall(Events.OnServerStarted.Add, function()
        local ok, err = pcall(startSession)
        if not ok then logError(err) end
    end)
end

-- Tiqueur. `OnTickEvenPaused` tire en tête de `IngameState.updateInternal`, avant que le
-- serveur ne teste `PauseEmpty` : il passe donc monde en pause, et la boucle lit `in`,
-- acquitte et bat même serveur vide. `OnTick`, sauté en pause, n'en est que le secours : dès
-- qu'`OnTickEvenPaused` a tiré une fois, il ne fait plus que réappliquer la tenue, après la
-- mise à jour de l'heure de la passe.
local evenPaused = false
if Events.OnTickEvenPaused and Events.OnTickEvenPaused.Add then
    pcall(Events.OnTickEvenPaused.Add, function()
        evenPaused = true
        runStep()
    end)
end

hooks.OnTick = Events.OnTick ~= nil
Events.OnTick.Add(function()
    if evenPaused then
        local ok, err = pcall(holdTick)
        if not ok then logError(err) end
        return
    end
    runStep()
end)
