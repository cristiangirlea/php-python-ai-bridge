<?php

declare(strict_types=1);

namespace PhpAiBridge;

/**
 * Carries one HTTP request to the worker. An implementation follows no redirect, takes no proxy from the
 * environment and refuses a body over MAX_RESPONSE_BYTES with BridgeException invalid_response; a failure to
 * reach the worker or read its answer is BridgeException transport_error. An expired timer that enforces
 * timeoutMs uses the internal TransportTimeout signal; other failures must not. The client judges status and body.
 */
interface Transport
{
    public const MAX_RESPONSE_BYTES = 262144;

    /**
     * @param non-empty-string $method
     * @param array<string, string> $headers
     * @param int $timeoutMs the whole request's budget, connection included
     * @throws BridgeException
     */
    public function send(string $method, string $url, #[\SensitiveParameter] array $headers, ?string $body, int $timeoutMs): TransportResponse;
}
