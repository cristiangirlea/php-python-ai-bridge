<?php

declare(strict_types=1);

// Boots a bare Symfony kernel, no FrameworkBundle, with only the bundle registered, and resolves the client from the
// compiled container and from the container that compilation dumped into the cache.

require __DIR__ . '/support.php';

use PhpAiBridge\Client;
use PhpAiBridge\Symfony\PhpAiBridgeBundle;
use Symfony\Component\Config\Loader\LoaderInterface;
use Symfony\Component\DependencyInjection\ContainerBuilder;
use Symfony\Component\HttpKernel\Kernel;

/** An application service that asks for the client by type only. */
final class BridgeConsumer
{
    public function __construct(public readonly Client $client) {}
}

final class TestKernel extends Kernel
{
    /**
     * @param array<string, mixed>|null $config the php_ai_bridge section, or null for none
     * @param bool $bundle false registers the client as the integration guide's manual services.yaml recipe does
     */
    public function __construct(
        private readonly string $dir,
        private readonly ?array $config = null,
        private readonly bool $bundle = true,
    ) {
        parent::__construct('test', false);
    }

    public function registerBundles(): iterable
    {
        if ($this->bundle) {
            yield new PhpAiBridgeBundle();
        }
    }

    public function registerContainerConfiguration(LoaderInterface $loader): void
    {
        $loader->load(function (ContainerBuilder $container): void {
            if ($this->config !== null) {
                $container->loadFromExtension('php_ai_bridge', $this->config);
            }
            if (!$this->bundle) {
                $container->setParameter('env(BRIDGE_REQUEST_TIMEOUT_MS)', '2000');
                $container->register(Client::class, Client::class)->setPublic(true)->setArguments([
                    '$baseUrl' => '%env(BRIDGE_URL)%',
                    '$token' => '%env(BRIDGE_TOKEN)%',
                    '$requestTimeoutMs' => '%env(int:BRIDGE_REQUEST_TIMEOUT_MS)%',
                ]);
            }
            $container->autowire(BridgeConsumer::class, BridgeConsumer::class)->setPublic(true);
        });
    }

    public function getProjectDir(): string
    {
        return $this->dir;
    }

    public function getCacheDir(): string
    {
        return $this->dir . '/cache';
    }

    public function getBuildDir(): string
    {
        return $this->dir . '/cache';
    }

    public function getLogDir(): string
    {
        return $this->dir . '/log';
    }
}

/** @param array<string, mixed>|null $config */
function symfonyKernel(string $dir, ?array $config = null, bool $bundle = true): TestKernel
{
    $kernel = new TestKernel($dir, $config, $bundle);
    try {
        $kernel->boot();
    } finally {
        promoteWarnings();
    }
    return $kernel;
}

function symfonyClient(TestKernel $kernel): Client
{
    $client = $kernel->getContainer()->get(Client::class);
    if (!$client instanceof Client) {
        throw new RuntimeException('The container did not return a Client');
    }
    return $client;
}

$token = token();
$tmp = static fn (): string => '/tmp/symfony-' . bin2hex(random_bytes(6));

// The first boot compiles the container with debug off and dumps it; the client reaches the worker.
$dir = $tmp();
$kernel = symfonyKernel($dir);
$client = symfonyClient($kernel);
check(symfonyClient($kernel) === $client, 'a second resolution returns the same client');
$consumer = $kernel->getContainer()->get(BridgeConsumer::class);
check($consumer instanceof BridgeConsumer && $consumer->client === $client, 'a service autowired by type receives the client');
rerankRoundTrip($client, 'Symfony');

// A second kernel on the same cache directory loads the dumped container instead of compiling again.
$dumped = glob($dir . '/cache/*.php') ?: [];
check($dumped !== [], 'the compiled container is dumped into the cache directory');
$times = array_map('filemtime', $dumped);
clearstatcache();
$again = symfonyKernel($dir);
check(array_map('filemtime', $dumped) === $times, 'the second kernel reads the dumped container unchanged');
check($again->getContainer() !== $kernel->getContainer(), 'the second kernel has its own container');
rerankRoundTrip(symfonyClient($again), 'Symfony from the dumped container');

// Values written in the configuration instead of the environment.
rerankRoundTrip(symfonyClient(symfonyKernel($tmp(), ['url' => 'http://worker:8090', 'token' => '%env(BRIDGE_TOKEN)%', 'request_timeout_ms' => 5000])), 'Symfony with explicit settings');

// The documented config/packages/php_ai_bridge.yaml: env placeholders written out compile, and the environment's
// values reach the client when it is resolved.
$documented = [
    'url' => '%env(default::BRIDGE_URL)%',
    'token' => '%env(default::BRIDGE_TOKEN)%',
    'request_timeout_ms' => '%env(int:BRIDGE_REQUEST_TIMEOUT_MS)%',
];
setEnv('BRIDGE_REQUEST_TIMEOUT_MS', '3000');
rerankRoundTrip(symfonyClient(symfonyKernel($tmp(), $documented)), 'Symfony with the documented configuration');
setEnv('BRIDGE_REQUEST_TIMEOUT_MS', null);
rerankRoundTrip(symfonyClient(symfonyKernel($tmp(), $documented)), 'Symfony with the documented configuration, timeout unset');
setEnv('BRIDGE_REQUEST_TIMEOUT_MS', '0');
$invalid = symfonyKernel($tmp(), $documented);
failsNaming(static fn () => symfonyClient($invalid), ['php_ai_bridge.request_timeout_ms', 'BRIDGE_REQUEST_TIMEOUT_MS'], [$token], 'Symfony with the documented configuration, timeout 0');
setEnv('BRIDGE_REQUEST_TIMEOUT_MS', null);

// The guide's manual services.yaml recipe, without the bundle: its env() default covers an unset timeout.
rerankRoundTrip(symfonyClient(symfonyKernel($tmp(), null, false)), 'Symfony with the manual service, timeout unset');
setEnv('BRIDGE_REQUEST_TIMEOUT_MS', '3000');
rerankRoundTrip(symfonyClient(symfonyKernel($tmp(), null, false)), 'Symfony with the manual service, timeout set');
setEnv('BRIDGE_REQUEST_TIMEOUT_MS', null);

// Invalid settings fail when the client is first resolved, naming the setting and never quoting the token.
$short = 'short-secret-value-' . bin2hex(random_bytes(4));
$cases = [
    'missing URL' => [['BRIDGE_URL' => null], ['php_ai_bridge.url', 'BRIDGE_URL']],
    'empty URL' => [['BRIDGE_URL' => ''], ['php_ai_bridge.url', 'BRIDGE_URL']],
    'URL with a path' => [['BRIDGE_URL' => 'http://worker:8090/api'], ['php_ai_bridge.url', 'BRIDGE_URL']],
    'missing token' => [['BRIDGE_TOKEN' => null], ['php_ai_bridge.token', 'BRIDGE_TOKEN']],
    'empty token' => [['BRIDGE_TOKEN' => ''], ['php_ai_bridge.token', 'BRIDGE_TOKEN']],
    'short token' => [['BRIDGE_TOKEN' => $short], ['php_ai_bridge.token', 'BRIDGE_TOKEN']],
    'timeout 0' => [['BRIDGE_REQUEST_TIMEOUT_MS' => '0'], ['php_ai_bridge.request_timeout_ms', 'BRIDGE_REQUEST_TIMEOUT_MS']],
    'timeout 300001' => [['BRIDGE_REQUEST_TIMEOUT_MS' => '300001'], ['php_ai_bridge.request_timeout_ms', 'BRIDGE_REQUEST_TIMEOUT_MS']],
    'timeout not a number' => [['BRIDGE_REQUEST_TIMEOUT_MS' => '5s'], ['BRIDGE_REQUEST_TIMEOUT_MS']],
];
foreach ($cases as $case => [$env, $names]) {
    foreach ($env as $name => $value) {
        setEnv($name, $value);
    }
    $invalid = symfonyKernel($tmp());
    check($invalid->getContainer()->has(Client::class), 'Symfony, ' . $case . ': the kernel boots before the client is resolved');
    failsNaming(static fn () => symfonyClient($invalid), $names, [$token, $short], 'Symfony, ' . $case);
    setEnv('BRIDGE_URL', 'http://worker:8090');
    setEnv('BRIDGE_TOKEN', $token);
    setEnv('BRIDGE_REQUEST_TIMEOUT_MS', null);
}

// A value written in the configuration is checked when the container is built.
failsNaming(static fn () => symfonyKernel($tmp(), ['url' => '']), ['php_ai_bridge.url'], [$token], 'Symfony, empty URL in the configuration');
failsNaming(static fn () => symfonyKernel($tmp(), ['request_timeout_ms' => 0]), ['php_ai_bridge.request_timeout_ms'], [$token], 'Symfony, timeout 0 in the configuration');
failsNaming(static fn () => symfonyKernel($tmp(), ['request_timeout_ms' => 300001]), ['php_ai_bridge.request_timeout_ms'], [$token], 'Symfony, timeout 300001 in the configuration');
failsNaming(static fn () => symfonyKernel($tmp(), ['request_timeout_ms' => '5000']), ['php_ai_bridge.request_timeout_ms'], [$token], 'Symfony, a quoted timeout in the configuration');
failsNaming(static fn () => symfonyKernel($tmp(), ['request_timeout_ms' => '%env(BRIDGE_REQUEST_TIMEOUT_MS)%']), ['php_ai_bridge.request_timeout_ms'], [$token], 'Symfony, a timeout env value without int:');

echo 'Symfony ' . Kernel::VERSION . ': ' . passed() . " checks passed\n";
