<?php

declare(strict_types=1);

namespace PhpAiBridge;

final class CurlTransport implements Transport
{
    public function send(string $method, string $url, #[\SensitiveParameter] array $headers, ?string $body, int $timeoutMs): TransportResponse
    {
        $handle = curl_init($url);
        $response = '';
        $tooLarge = false;
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
        ]);
        if ($body !== null) {
            curl_setopt($handle, CURLOPT_POSTFIELDS, $body);
        }
        try {
            $ok = curl_exec($handle);
            $status = (int) curl_getinfo($handle, CURLINFO_RESPONSE_CODE);
            $contentType = curl_getinfo($handle, CURLINFO_CONTENT_TYPE);
            if ($tooLarge) {
                throw new BridgeException('Response exceeds ' . self::MAX_RESPONSE_BYTES . ' bytes', 'invalid_response');
            }
            if ($ok === false) {
                throw new BridgeException('Bridge transport failed; submission outcome may be unknown', 'transport_error');
            }
            return new TransportResponse($status, is_string($contentType) ? $contentType : null, $response);
        } finally {
            // PHP 8 releases CurlHandle objects; curl_close() is deprecated in 8.5.
            unset($handle);
        }
    }
}
