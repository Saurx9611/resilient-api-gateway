-- KEYS: identity state hash, route identity-expiry registry, route policy.
-- ARGV: limit, window milliseconds, identity cap, SHA-256 peer identity.
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local cap = tonumber(ARGV[3])
local identity = ARGV[4]
local timestamp = redis.call('TIME')
local now = tonumber(timestamp[1]) * 1000 + math.floor(tonumber(timestamp[2]) / 1000)
local bucket = math.floor(now / window)
local elapsed = now - bucket * window
local signature = ARGV[1] .. ':' .. ARGV[2] .. ':' .. ARGV[3]
local policy = redis.call('GET', KEYS[3])
if policy and policy ~= signature then
    return redis.error_reply('LIMITER_POLICY_MISMATCH')
end

local stored = redis.call('HMGET', KEYS[1], 'bucket', 'current', 'previous')
local current, previous = 0, 0
if stored[1] then
    local old = tonumber(stored[1])
    current, previous = tonumber(stored[2]), tonumber(stored[3])
    if not old or not current or not previous or current < 0 or previous < 0
        or current > limit or previous > limit then
        return redis.error_reply('LIMITER_INVALID_STATE')
    end
    if bucket < old then return redis.error_reply('LIMITER_CLOCK_ROLLBACK') end
    if bucket == old + 1 then
        previous, current = current, 0
    elseif bucket > old + 1 then
        current, previous = 0, 0
    end
end

redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
if not redis.call('ZSCORE', KEYS[2], identity) and redis.call('ZCARD', KEYS[2]) >= cap then
    local first = redis.call('ZRANGE', KEYS[2], 0, 0, 'WITHSCORES')
    return {2, 0, 0, math.max(1, tonumber(first[2]) - now), now}
end

local scaled = current * window + previous * (window - elapsed)
local allowed = 0
local retry = 0
if scaled + window <= limit * window then
    allowed = 1
    current = current + 1
    scaled = scaled + window
    local expiry = (bucket + 2) * window
    -- Register first: a later write error consumes a slot conservatively.
    redis.call('ZADD', KEYS[2], expiry, identity)
    redis.call('PEXPIREAT', KEYS[2], now + 2 * window)
    redis.call('SET', KEYS[3], signature, 'PX', 2 * window)
    redis.call('HSET', KEYS[1], 'bucket', bucket, 'current', current, 'previous', previous)
    redis.call('PEXPIREAT', KEYS[1], expiry)
elseif current < limit and previous > 0 then
    -- Solve (current+previous+1-limit)*window - previous*elapsed <= 0.
    retry = math.max(1, math.ceil((current + previous + 1 - limit) * window / previous) - elapsed)
else
    -- A full current bucket needs rollover AND some decay in the next bucket.
    retry = window - elapsed + math.ceil((current + 1 - limit) * window / current)
end

local reset = 0
if current > 0 then reset = 2 * window - elapsed
elseif previous > 0 then reset = window - elapsed end
local remaining = math.max(0, math.floor((limit * window - scaled) / window))
return {allowed, remaining, reset, retry, now}
