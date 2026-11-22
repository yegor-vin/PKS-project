
local fiit = Proto("fiitmeteo", "FIITMeteo Sensor Protocol")

local TYPES        = { [0] = "REGISTER", [1] = "ACK", [2] = "REJECT",
                       [3] = "DATA", [4] = "RETRANSMIT", [5] = "KEEPALIVE" }
local SENSOR_TYPES = { [0] = "ThermoNode", [1] = "WindSense",
                       [2] = "RainDetect", [3] = "AirQuality" }
local REASONS      = { [0] = "Invalid sensor", [1] = "Already registered",
                       [2] = "Invalid token", [3] = "Token does not match sensor",
                       [4] = "Missing values" }

local SPECS = {
    ThermoNode = { {"temperature", -50, 60, 1}, {"humidity", 0, 100, 1},
                   {"dew_point", -50, 60, 1},   {"pressure", 800, 1100, 2} },
    WindSense  = { {"wind_speed", 0, 50, 1},    {"wind_gust", 0, 70, 1},
                   {"wind_direction", 0, 359, 0}, {"turbulence", 0, 1, 1} },
    RainDetect = { {"rainfall", 0, 500, 1},     {"soil_moisture", 0, 100, 1},
                   {"flood_risk", 0, 3, 0},     {"rain_duration", 0, 60, 0} },
    AirQuality = { {"co2", 300, 5000, 0},       {"ozone", 0, 500, 1},
                   {"air_quality_index", 0, 500, 0} },
}

local f = {
    type        = ProtoField.uint8 ("type", "Type", base.DEC, TYPES, 0xE0),
    sensor_type = ProtoField.uint8 ("sensor_type", "Sensor type", base.DEC, SENSOR_TYPES, 0x18),
    flags       = ProtoField.uint8 ("flags", "Flags", base.HEX, nil, 0x07),
    sequence    = ProtoField.uint16("sequence", "Sequence", base.DEC),
    ack_for     = ProtoField.uint16("ack_for", "ACK for sequence", base.DEC),
    token       = ProtoField.uint32("token", "Token", base.HEX),
    sensor_id   = ProtoField.uint8 ("sensor_id", "Sensor ID", base.DEC),
    reason      = ProtoField.uint8 ("reason", "Reject reason", base.DEC, REASONS),
    values      = ProtoField.bytes ("values", "Packed values"),
    padding     = ProtoField.uint8 ("padding_bits", "Padding bits", base.DEC),
    crc         = ProtoField.uint16("crc", "CRC-16", base.HEX),
    crc_calc    = ProtoField.uint16("crc_calculated", "CRC-16 calculated", base.HEX),
    crc_ok      = ProtoField.bool  ("crc_ok", "CRC OK"),
}

local value_fields = {}
for _, spec in pairs(SPECS) do
    for _, s in ipairs(spec) do
        local name = s[1]
        if s[4] == 0 then
            value_fields[name] = ProtoField.int32("fiitmeteo." .. name, name, base.DEC)
        else
            value_fields[name] = ProtoField.double("fiitmeteo." .. name, name)
        end
    end
end

local plist = {}
for _, v in pairs(f) do plist[#plist + 1] = v end
for _, v in pairs(value_fields) do plist[#plist + 1] = v end
fiit.fields = plist

local ef_bad_crc   = ProtoExpert.new("crc.bad", "CRC mismatch - message damaged",
                                     expert.group.CHECKSUM, expert.severity.ERROR)
local ef_malformed = ProtoExpert.new("malformed", "Malformed FIITMeteo message",
                                     expert.group.MALFORMED, expert.severity.ERROR)
local ef_range     = ProtoExpert.new("range", "Value outside sensor range",
                                     expert.group.PROTOCOL, expert.severity.WARN)
fiit.experts = { ef_bad_crc, ef_malformed, ef_range }

fiit.prefs.port = Pref.uint("UDP port", 5500, "UDP port of the FIITMeteo server")

local function xor16(a, b)
    local r, p = 0, 1
    for _ = 1, 16 do
        if (a % 2) ~= (b % 2) then r = r + p end
        a = math.floor(a / 2); b = math.floor(b / 2); p = p * 2
    end
    return r
end

local function crc16(tvb, offset, length)
    local crc = 0xFFFF
    for i = offset, offset + length - 1 do
        crc = xor16(crc, tvb(i, 1):uint() * 256)
        for _ = 1, 8 do
            if crc >= 0x8000 then
                crc = xor16((crc * 2) % 0x10000, 0x1021)
            else
                crc = (crc * 2) % 0x10000
            end
        end
    end
    return crc
end

local function bits_for(low, high, decimals)
    local n = math.floor((high - low) * 10 ^ decimals + 0.5)
    local w = 0
    while 2 ^ w < n + 1 do w = w + 1 end
    return w
end

local function read_bits(tvb, offset, pos, width)
    local v = 0
    for i = pos, pos + width - 1 do
        local byte = tvb(offset + math.floor(i / 8), 1):uint()
        local bit = math.floor(byte / 2 ^ (7 - i % 8)) % 2
        v = v * 2 + bit
    end
    return v
end

local function fmt(value, decimals)
    if decimals == 0 then return string.format("%d", value) end
    return string.format("%." .. decimals .. "f", value)
end

local function dissect_values(tvb, pinfo, tree, offset, length, sensor_type)
    local spec = SPECS[sensor_type]
    local total = 0
    for _, s in ipairs(spec) do total = total + bits_for(s[2], s[3], s[4]) end
    local nbytes = math.ceil(total / 8)
    if length < nbytes then
        tree:add_proto_expert_info(ef_malformed, "Values too short for " .. sensor_type)
        return ""
    end

    local vt = tree:add(f.values, tvb(offset, nbytes))
    vt:append_text(string.format(" (%d bits for %s)", total, sensor_type))
    local pos, summary = 0, {}
    for _, s in ipairs(spec) do
        local name, low, high, dec = s[1], s[2], s[3], s[4]
        local w = bits_for(low, high, dec)
        local raw = read_bits(tvb, offset, pos, w)
        local value = low + raw / 10 ^ dec
        local first, last = math.floor(pos / 8), math.floor((pos + w - 1) / 8)
        local item = vt:add(value_fields[name], tvb(offset + first, last - first + 1), value)
        item:set_text(string.format("%s: %s  (raw %d, %d bits, bits %d-%d)",
                                    name, fmt(value, dec), raw, w, pos, pos + w - 1))
        if value > high then item:add_proto_expert_info(ef_range) end
        summary[#summary + 1] = name .. "=" .. fmt(value, dec)
        pos = pos + w
    end
    vt:add(f.padding, tvb(offset + nbytes - 1, 1), nbytes * 8 - total)
    return table.concat(summary, " ")
end


local function dissect_body(tvb, pinfo, tree, mtype, sensor_type, offset, length)
    if mtype == "REGISTER" then
        if length ~= 1 then return nil end
        tree:add(f.sensor_id, tvb(offset, 1))
        return string.format("%s:%d", sensor_type, tvb(offset, 1):uint())

    elseif mtype == "ACK" or mtype == "RETRANSMIT" then
        if length ~= 2 and length ~= 6 then return nil end
        tree:add(f.ack_for, tvb(offset, 2))
        local info = "ack-for=" .. tvb(offset, 2):uint()
        if length == 6 then
            tree:add(f.token, tvb(offset + 2, 4))
            info = info .. string.format(" token=%08x", tvb(offset + 2, 4):uint())
        end
        return info

    elseif mtype == "REJECT" then
        if length ~= 3 then return nil end
        tree:add(f.ack_for, tvb(offset, 2))
        tree:add(f.reason, tvb(offset + 2, 1))
        local r = REASONS[tvb(offset + 2, 1):uint()] or "unknown"
        return string.format("ack-for=%d reason=\"%s\"", tvb(offset, 2):uint(), r)

    elseif mtype == "KEEPALIVE" then
        if length ~= 5 then return nil end
        tree:add(f.token, tvb(offset, 4))
        tree:add(f.sensor_id, tvb(offset + 4, 1))
        return string.format("token=%08x id=%d", tvb(offset, 4):uint(), tvb(offset + 4, 1):uint())

    elseif mtype == "DATA" then
        if length < 6 then return nil end
        tree:add(f.token, tvb(offset, 4))
        tree:add(f.sensor_id, tvb(offset + 4, 1))
        local values = dissect_values(tvb, pinfo, tree, offset + 5, length - 5, sensor_type)
        return string.format("%s:%d %s", sensor_type, tvb(offset + 4, 1):uint(), values)
    end
    return nil
end

function fiit.dissector(tvb, pinfo, tree)
    local len = tvb:len()
    if len < 5 then return 0 end
    local b0 = tvb(0, 1):uint()
    local t  = math.floor(b0 / 32)
    local st = math.floor(b0 / 8) % 4
    local mtype, sensor_type = TYPES[t], SENSOR_TYPES[st]
    if mtype == nil then return 0 end

    pinfo.cols.protocol = "FIITMETEO"
    local subtree = tree:add(fiit, tvb(), string.format("FIITMeteo %s, Seq %d",
                             mtype, tvb(1, 2):uint()))

    local ht = subtree:add(fiit, tvb(0, 3), "Header (3 bytes)")
    ht:add(f.type, tvb(0, 1))
    ht:add(f.sensor_type, tvb(0, 1))
    ht:add(f.flags, tvb(0, 1))
    ht:add(f.sequence, tvb(1, 2))

    local body_len = len - 5
    local bt = subtree:add(fiit, tvb(3, math.max(body_len, 0)),
                           string.format("Body (%d bytes)", body_len))
    local info = dissect_body(tvb, pinfo, bt, mtype, sensor_type, 3, body_len)
    if info == nil then
        bt:add_proto_expert_info(ef_malformed, "Unexpected body length " .. body_len .. " for " .. mtype)
        info = "malformed"
    end

    local crc = tvb(len - 2, 2):uint()
    local calc = crc16(tvb, 0, len - 2)
    local ok = (crc == calc)
    local ct = subtree:add(f.crc, tvb(len - 2, 2))
    ct:append_text(ok and " [correct]" or string.format(" [INCORRECT, should be 0x%04x]", calc))
    ct:add(f.crc_calc, calc):set_generated()
    ct:add(f.crc_ok, ok):set_generated()
    if not ok then ct:add_proto_expert_info(ef_bad_crc) end

    pinfo.cols.info = string.format("%-10s seq=%-5d %s%s", mtype, tvb(1, 2):uint(), info,
                                    ok and "" or "  [BAD CRC]")
    return len
end

local udp_table = DissectorTable.get("udp.port")
local current_port = fiit.prefs.port
udp_table:add(current_port, fiit)

function fiit.prefs_changed()
    if current_port ~= fiit.prefs.port then
        udp_table:remove(current_port, fiit)
        current_port = fiit.prefs.port
        udp_table:add(current_port, fiit)
    end
end