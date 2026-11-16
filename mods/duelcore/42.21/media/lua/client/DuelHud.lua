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

local function onServerCommand(moduleName, command, args)
    if moduleName ~= MODULE or command ~= ANNOUNCE then return end
    if type(args) ~= "table" or type(args.text) ~= "string" then return end
    local found, player = pcall(getPlayer)
    if not found or not player then return end
    local shown = HaloTextHelper ~= nil and show(player, args.text, args.tone)
    reply(player, args.seq, shown == true)
end

Events.OnServerCommand.Add(function(moduleName, command, args)
    pcall(onServerCommand, moduleName, command, args)
end)
