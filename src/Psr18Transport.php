<?php

declare(strict_types=1);

namespace PhpAiBridge;

use Psr\Http\Client\ClientInterface;
use Psr\Http\Message\RequestFactoryInterface;
use Psr\Http\Message\StreamFactoryInterface;

/**
 * Sends the bridge's requests through an application's PSR-18 client; needs psr/http-client and psr/http-factory.
 * Configure that client with redirects off and no proxy for the worker: a 3xx answer is refused here either way.
 * PSR-18 has no per-request timeout, so the client's own timeout applies and $timeoutMs is not enforced.
 */
final class Psr18Transport implements Transport
{
    public function __construct(
        private readonly ClientInterface $client,
        private readonly RequestFactoryInterface $requestFactory,
        private readonly StreamFactoryInterface $streamFactory,
    ) {
    }

    public function send(string $method, string $url, #[\SensitiveParameter] array $headers, ?string $body, int $timeoutMs): TransportResponse
    {
        $request = $this->requestFactory->createRequest($method, $url);
        foreach ($headers as $name => $value) {
            $request = $request->withHeader($name, $value);
        }
        if ($body !== null) {
            $request = $request->withBody($this->streamFactory->createStream($body));
        }
        try {
            $response = $this->client->sendRequest($request);
            $stream = $response->getBody();
            try {
                $status = $response->getStatusCode();
                if ($status >= 300 && $status < 400) {
                    throw new BridgeException('Bridge answered with a redirect, which the client never follows', 'invalid_response');
                }
                $content = '';
                while (strlen($content) <= self::MAX_RESPONSE_BYTES && !$stream->eof()) {
                    $chunk = $stream->read(self::MAX_RESPONSE_BYTES + 1 - strlen($content));
                    if ($chunk === '') {
                        break;
                    }
                    $content .= $chunk;
                }
                if (strlen($content) > self::MAX_RESPONSE_BYTES) {
                    throw new BridgeException('Response exceeds ' . self::MAX_RESPONSE_BYTES . ' bytes', 'invalid_response');
                }
                $contentType = $response->getHeaderLine('Content-Type');
                return new TransportResponse($status, $contentType === '' ? null : $contentType, $content);
            } finally {
                $stream->close();
            }
        } catch (BridgeException $error) {
            throw $error;
        } catch (\Throwable) {
            // PSR-7 and PSR-18 code may throw anything, and its message can name hosts or carry the peer's text.
            throw new BridgeException('Bridge transport failed; submission outcome may be unknown', 'transport_error');
        }
    }
}
