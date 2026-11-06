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
--                                                mise à jour tant que la plaie saigne
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
local MIN_DISTANCE = 25
local PLACE_ATTEMPTS = 32
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
    return true
end

local function flushOut()
    if #outLines == 0 then return end
    local writer = getFileWriter(OUT_FILE, true, true)
    if not writer then return end
    writer:write(table.concat(outLines))
    writer:close()
    outLines = {}
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

-- La preuve est une blessure reçue, pas une réception quelconque du hook.
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
        })), side)
    end
    noteHook(hook)
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

-- Une case libre à une distance du joueur comprise dans `distance`, ou nil.
local function pickSquare(player, distance)
    local low, high = distance[1], distance[2]
    local px, py = player:getX(), player:getY()
    for _ = 1, PLACE_ATTEMPTS do
        local angle = math.rad(ZombRand(360))
        local radius = ZombRand(low, high + 1)
        local x = math.floor(px + math.cos(angle) * radius)
        local y = math.floor(py + math.sin(angle) * radius)
        local gap = distanceBetween(x, y, px, py)
        if gap >= low and gap <= high then
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

local function spawnAt(square, count, prepare)
    local zombies = addZombiesInOutfit(square:getX(), square:getY(), 0, count, nil, nil)
    if not zombies then return 0 end
    local spawned = zombies:size()
    for i = 0, spawned - 1 do
        local zombie = zombies:get(i)
        zombie:getModData()[DUEL_MARK] = true
        if prepare then prepare(zombie) end
    end
    return spawned
end

local function spawnResult(spawned)
    if spawned == 0 then
        return { status = "failed", reason = "no_spawn", spawned = 0 }
    end
    return { status = "applied", spawned = spawned }
end

local NO_SQUARE = { status = "failed", reason = "no_square" }

-- Traitements par `kind` ----------------------------------------------------------

local handlers = {}

local function spawnGroup(cmd, player, prepare)
    local square = pickSquare(player, cmd.distance)
    if not square then return NO_SQUARE end
    return spawnResult(spawnAt(square, needNumber(cmd, "n"), prepare))
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
        if square then spawned = spawned + spawnAt(square, 1, nil) end
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
    addAtom("damage_taken", hooks.OnPlayerGetDamage)
    addAtom("build", hooks.OnProcessAction)
    addAtom("vehicle_repair", repairClasses > 0)
    addAtom("explosion", hooks.OnThrowableExplode)

    emit("caps", {
        build = getCore():getGameAndBuildVersion(),
        mod = MOD_VERSION,
        protocol = PROTOCOL,
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
    local player = onlinePlayers()[1]
    local reason = refusalReason(player, cmd)
    if reason then
        return { status = "refused", reason = reason }
    end
    return handler(cmd, player)
end

local function emitResult(seq, result)
    emit("cmd_result", {
        seq = seq,
        status = result.status,
        reason = result.reason,
        spawned = result.spawned,
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

hooks.OnTick = Events.OnTick ~= nil
Events.OnTick.Add(function()
    local ok, err = pcall(step)
    if not ok then logError(err) end
end)
