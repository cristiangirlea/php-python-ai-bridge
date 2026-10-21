<?php

declare(strict_types=1);

namespace PhpAiBridge;

final class CurlTransport implements Transport
{
    private const MAX_HEADER_BYTES = 16384;

    public function send(string $method, string $url, #[\SensitiveParameter] array $headers, ?string $body, int $timeoutMs): TransportResponse
    {
        $handle = curl_init($url);
        $response = '';
        $tooLarge = false;
        $headerBytes = 0;
        $headersTooLarge = false;
        $retryAfter = null;
        $retryAfterSeen = false;
        $lines = [];
        foreach ($headers as $name => $value) {
            $lines[] = $name . ': ' . $value;
        }
        curl_setopt_array($handle, [
            CURLOPT_CUSTOMREQUEST => $method,
            CURLOPT_HTTPHEADER => $lines,
            CURLOPT_FOLLOWLOCATION => false,
            CURLOPT_PROTOCOLS => CURLPROTO_HTTP | CURLPROTO_HTTPS,
            CURLOPT_CONNECTTIMEOUT_MS => min(1000, $timeoutMs),
            CURLOPT_TIMEOUT_MS => $timeoutMs,
            CURLOPT_NOSIGNAL => true,
            CURLOPT_PROXY => '',
            CURLOPT_WRITEFUNCTION => static function ($curl, string $chunk) use (&$response, &$tooLarge): int {
                if (strlen($response) + strlen($chunk) > self::MAX_RESPONSE_BYTES) {
                    $tooLarge = true;
                    return 0;
                }
                $response .= $chunk;
                return strlen($chunk);
            },
            CURLOPT_HEADERFUNCTION => static function ($curl, string $line) use (&$headerBytes, &$headersTooLarge, &$retryAfter, &$retryAfterSeen): int {
                // An interim response such as 100 Continue starts a new block; only the final one counts.
                if (str_starts_with($line, 'HTTP/')) {
                    $headerBytes = 0;
                    $retryAfter = null;
                    $retryAfterSeen = false;
                }
                $headerBytes += strlen($line);
                if ($headerBytes > self::MAX_HEADER_BYTES) {
                    $headersTooLarge = true;
                    return 0;
                }
                if (preg_match('/\Aretry-after:(.*)\z/is', $line, $match) === 1) {
                    // Delay seconds only: an HTTP date would need this host's clock to agree with the worker's.
                    // A repeated header is a conflict the caller cannot trust, so it counts as absent.
                    $retryAfter = !$retryAfterSeen && preg_match('/\A[ \t]*([0-9]{1,6})[ \t]*\r?\n?\z/', $match[1], $digits) === 1 ? (int) $digits[1] : null;
                    $retryAfterSeen = true;
                }
                return strlen($line);
            },
        ]);
        if ($body !== null) {
            curl_setopt($handle, CURLOPT_POSTFIELDS, $body);
        }
        try {
            $ok = curl_exec($handle);
            $status = (int) curl_getinfo($handle, CURLINFO_RESPONSE_CODE);
            $contentType = curl_getinfo($handle, CURLINFO_CONTENT_TYPE);
            if ($headersTooLarge) {
                throw new BridgeException('Response headers exceed ' . self::MAX_HEADER_BYTES . ' bytes', 'invalid_response');
            }
            if ($tooLarge) {
                throw new BridgeException('Response exceeds ' . self::MAX_RESPONSE_BYTES . ' bytes', 'invalid_response');
            }
            if ($ok === false) {
                throw new BridgeException('Bridge transport failed; submission outcome may be unknown', 'transport_error');
            }
            return new TransportResponse($status, is_string($contentType) ? $contentType : null, $response, $retryAfter);
        } finally {
            // PHP 8 releases CurlHandle objects; curl_close() is deprecated in 8.5.
            unset($handle);
        }
    }
}
