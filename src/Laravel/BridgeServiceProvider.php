<?php

declare(strict_types=1);

namespace PhpAiBridge\Laravel;

use Illuminate\Contracts\Config\Repository;
use Illuminate\Contracts\Container\Container;
use Illuminate\Support\ServiceProvider;
use PhpAiBridge\Client;

/**
 * Binds one Client built from config/ai_bridge.php. Laravel discovers it from composer.json; the class exists for
 * Laravel applications only, and nothing else in the library loads it.
 */
final class BridgeServiceProvider extends ServiceProvider
{
    private const SETTINGS = [
        'url' => 'ai_bridge.url (BRIDGE_URL)',
        'token' => 'ai_bridge.token (BRIDGE_TOKEN)',
        'request_timeout_ms' => 'ai_bridge.request_timeout_ms (BRIDGE_REQUEST_TIMEOUT_MS)',
    ];

    public function register(): void
    {
        $this->mergeConfigFrom(__DIR__ . '/config/ai_bridge.php', 'ai_bridge');
        $this->app->singleton(Client::class, static function (Container $app): Client {
            $config = $app->make(Repository::class);
            return self::client($config->get('ai_bridge.url'), $config->get('ai_bridge.token'), $config->get('ai_bridge.request_timeout_ms'));
        });
    }

    public function boot(): void
    {
        $this->publishes([__DIR__ . '/config/ai_bridge.php' => $this->app->configPath('ai_bridge.php')], 'ai-bridge-config');
    }

    /** The values' own checks are the client's; this names the setting a failure came from. */
    private static function client(mixed $url, #[\SensitiveParameter] mixed $token, mixed $requestTimeoutMs): Client
    {
        if (!is_string($url) || $url === '') {
            throw new \InvalidArgumentException(self::SETTINGS['url'] . ' is not set');
        }
        if (!is_string($token) || $token === '') {
            throw new \InvalidArgumentException(self::SETTINGS['token'] . ' is not set');
        }
        // env() returns the variable as a string, and a cached configuration keeps it so.
        if (is_string($requestTimeoutMs) && preg_match('/^[0-9]{1,9}$/', $requestTimeoutMs) === 1) {
            $requestTimeoutMs = (int) $requestTimeoutMs;
        }
        if (!is_int($requestTimeoutMs)) {
            throw new \InvalidArgumentException(self::SETTINGS['request_timeout_ms'] . ' must be an integer');
        }
        try {
            return new Client($url, $token, $requestTimeoutMs);
        } catch (\InvalidArgumentException $error) {
            $key = match (true) {
                str_starts_with($error->getMessage(), 'baseUrl') => 'url',
                str_starts_with($error->getMessage(), 'token') => 'token',
                default => 'request_timeout_ms',
            };
            throw new \InvalidArgumentException(self::SETTINGS[$key] . ': ' . $error->getMessage(), 0, $error);
        }
    }
}
