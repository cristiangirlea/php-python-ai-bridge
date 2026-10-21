<?php

declare(strict_types=1);

namespace PhpAiBridge\Symfony;

use PhpAiBridge\Client;
use Symfony\Component\Config\Definition\Configurator\DefinitionConfigurator;
use Symfony\Component\DependencyInjection\ContainerBuilder;
use Symfony\Component\DependencyInjection\Loader\Configurator\ContainerConfigurator;
use Symfony\Component\HttpKernel\Bundle\AbstractBundle;

/**
 * Registers Client as an autowirable service configured under php_ai_bridge, from the environment by default. The
 * class exists for Symfony applications only, and nothing else in the library loads it.
 */
final class PhpAiBridgeBundle extends AbstractBundle
{
    private const SETTINGS = [
        'url' => 'php_ai_bridge.url (BRIDGE_URL by default)',
        'token' => 'php_ai_bridge.token (BRIDGE_TOKEN by default)',
        'request_timeout_ms' => 'php_ai_bridge.request_timeout_ms (BRIDGE_REQUEST_TIMEOUT_MS by default)',
    ];

    protected string $extensionAlias = 'php_ai_bridge';

    public function configure(DefinitionConfigurator $definition): void
    {
        // Environment values are known only at runtime, so createClient() checks them; written values fail the build.
        $definition->rootNode()->children()
            ->scalarNode('url')->defaultValue('%env(default::BRIDGE_URL)%')->cannotBeEmpty()->end()
            ->scalarNode('token')->defaultValue('%env(default::BRIDGE_TOKEN)%')->cannotBeEmpty()->end()
            ->scalarNode('request_timeout_ms')->defaultValue('%env(int:BRIDGE_REQUEST_TIMEOUT_MS)%')
                ->validate()
                    ->ifTrue(static fn (mixed $value): bool => is_int($value) && ($value < 1 || $value > 300000))
                    ->thenInvalid('php_ai_bridge.request_timeout_ms must be between 1 and 300000, got %s')
                ->end()
            ->end()
        ->end();
    }

    public function prependExtension(ContainerConfigurator $container, ContainerBuilder $builder): void
    {
        // The client's own default when BRIDGE_REQUEST_TIMEOUT_MS is unset; an application parameter still wins.
        if (!$builder->hasParameter('env(BRIDGE_REQUEST_TIMEOUT_MS)')) {
            $builder->setParameter('env(BRIDGE_REQUEST_TIMEOUT_MS)', '2000');
        }
    }

    /** @param array<string, mixed> $config */
    public function loadExtension(array $config, ContainerConfigurator $container, ContainerBuilder $builder): void
    {
        $container->services()
            ->set(Client::class)
            ->factory([self::class, 'createClient'])
            ->args([$config['url'], $config['token'], $config['request_timeout_ms']])
            ->public();
    }

    /** The container's factory for Client. The values' own checks are the client's; this names the setting a failure came from. */
    public static function createClient(mixed $url, #[\SensitiveParameter] mixed $token, mixed $requestTimeoutMs): Client
    {
        if (!is_string($url) || $url === '') {
            throw new \InvalidArgumentException(self::SETTINGS['url'] . ' is not set');
        }
        if (!is_string($token) || $token === '') {
            throw new \InvalidArgumentException(self::SETTINGS['token'] . ' is not set');
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
