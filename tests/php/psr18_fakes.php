<?php

declare(strict_types=1);

// The smallest PSR-7, PSR-17 and PSR-18 classes Psr18Transport needs; run.php loads them only when the interfaces exist.

namespace BridgeTests;

use Psr\Http\Client\ClientExceptionInterface;
use Psr\Http\Client\ClientInterface;
use Psr\Http\Message\MessageInterface;
use Psr\Http\Message\RequestFactoryInterface;
use Psr\Http\Message\RequestInterface;
use Psr\Http\Message\ResponseInterface;
use Psr\Http\Message\StreamFactoryInterface;
use Psr\Http\Message\StreamInterface;
use Psr\Http\Message\UriInterface;

final class FakeStream implements StreamInterface
{
    public int $bytesRead = 0;
    public int $badReads = 0;
    public bool $closed = false;
    private int $position = 0;

    public function __construct(private readonly string $content = '', private readonly ?\Throwable $readFailure = null)
    {
    }

    public function __toString(): string
    {
        return $this->content;
    }

    public function close(): void
    {
        $this->closed = true;
    }

    public function detach()
    {
        return null;
    }

    public function getSize(): int
    {
        return strlen($this->content);
    }

    public function tell(): int
    {
        return $this->position;
    }

    public function eof(): bool
    {
        return $this->position >= strlen($this->content);
    }

    public function isSeekable(): bool
    {
        return false;
    }

    public function seek(int $offset, int $whence = SEEK_SET): void
    {
        throw new \RuntimeException('Not seekable');
    }

    public function rewind(): void
    {
        throw new \RuntimeException('Not seekable');
    }

    public function isWritable(): bool
    {
        return false;
    }

    public function write(string $string): int
    {
        throw new \RuntimeException('Not writable');
    }

    public function isReadable(): bool
    {
        return true;
    }

    public function read(int $length): string
    {
        // Counted as well as thrown, since the transport turns any read failure into transport_error.
        if ($length < 1) {
            $this->badReads++;
            throw new \LogicException('read() needs a positive length');
        }
        if ($this->readFailure !== null) {
            throw $this->readFailure;
        }
        $chunk = substr($this->content, $this->position, min($length, 8192));
        $this->position += strlen($chunk);
        $this->bytesRead += strlen($chunk);
        return $chunk;
    }

    public function getContents(): string
    {
        $rest = '';
        while (!$this->eof()) {
            $rest .= $this->read(8192);
        }
        return $rest;
    }

    public function getMetadata(?string $key = null)
    {
        return $key === null ? [] : null;
    }
}

final class FakeUri implements UriInterface
{
    public function __construct(private readonly string $uri)
    {
    }

    public function getScheme(): string
    {
        return (string) parse_url($this->uri, PHP_URL_SCHEME);
    }

    public function getAuthority(): string
    {
        return $this->getHost();
    }

    public function getUserInfo(): string
    {
        return '';
    }

    public function getHost(): string
    {
        return (string) parse_url($this->uri, PHP_URL_HOST);
    }

    public function getPort(): ?int
    {
        $port = parse_url($this->uri, PHP_URL_PORT);
        return is_int($port) ? $port : null;
    }

    public function getPath(): string
    {
        return (string) parse_url($this->uri, PHP_URL_PATH);
    }

    public function getQuery(): string
    {
        return '';
    }

    public function getFragment(): string
    {
        return '';
    }

    public function withScheme(string $scheme): UriInterface
    {
        return $this;
    }

    public function withUserInfo(string $user, ?string $password = null): UriInterface
    {
        return $this;
    }

    public function withHost(string $host): UriInterface
    {
        return $this;
    }

    public function withPort(?int $port): UriInterface
    {
        return $this;
    }

    public function withPath(string $path): UriInterface
    {
        return $this;
    }

    public function withQuery(string $query): UriInterface
    {
        return $this;
    }

    public function withFragment(string $fragment): UriInterface
    {
        return $this;
    }

    public function __toString(): string
    {
        return $this->uri;
    }
}

/** Header names are kept lower-case, which PSR-7 allows since it reads them case-insensitively. */
trait FakeMessage
{
    /** @var array<string, list<string>> */
    private array $headers = [];
    private ?StreamInterface $body = null;

    public function getProtocolVersion(): string
    {
        return '1.1';
    }

    public function withProtocolVersion(string $version): MessageInterface
    {
        return $this;
    }

    /** @return array<string, list<string>> */
    public function getHeaders(): array
    {
        return $this->headers;
    }

    public function hasHeader(string $name): bool
    {
        return isset($this->headers[strtolower($name)]);
    }

    /** @return list<string> */
    public function getHeader(string $name): array
    {
        return $this->headers[strtolower($name)] ?? [];
    }

    public function getHeaderLine(string $name): string
    {
        return implode(', ', $this->getHeader($name));
    }

    /** @param string|list<string> $value */
    public function withHeader(string $name, $value): MessageInterface
    {
        $copy = clone $this;
        $copy->headers[strtolower($name)] = is_array($value) ? $value : [$value];
        return $copy;
    }

    /** @param string|list<string> $value */
    public function withAddedHeader(string $name, $value): MessageInterface
    {
        $copy = clone $this;
        $copy->headers[strtolower($name)] = [...$this->getHeader($name), ...(is_array($value) ? $value : [$value])];
        return $copy;
    }

    public function withoutHeader(string $name): MessageInterface
    {
        $copy = clone $this;
        unset($copy->headers[strtolower($name)]);
        return $copy;
    }

    public function getBody(): StreamInterface
    {
        return $this->body ??= new FakeStream();
    }

    public function withBody(StreamInterface $body): MessageInterface
    {
        $copy = clone $this;
        $copy->body = $body;
        return $copy;
    }
}

final class FakeRequest implements RequestInterface
{
    use FakeMessage;

    public function __construct(private string $method, private UriInterface $uri)
    {
    }

    public function getRequestTarget(): string
    {
        return $this->uri->getPath();
    }

    public function withRequestTarget(string $requestTarget): RequestInterface
    {
        return $this;
    }

    public function getMethod(): string
    {
        return $this->method;
    }

    public function withMethod(string $method): RequestInterface
    {
        $copy = clone $this;
        $copy->method = $method;
        return $copy;
    }

    public function getUri(): UriInterface
    {
        return $this->uri;
    }

    public function withUri(UriInterface $uri, bool $preserveHost = false): RequestInterface
    {
        $copy = clone $this;
        $copy->uri = $uri;
        return $copy;
    }
}

final class FakeResponse implements ResponseInterface
{
    use FakeMessage;

    /** @param array<string, string> $headers */
    public function __construct(private int $status, array $headers = [], ?StreamInterface $body = null)
    {
        foreach ($headers as $name => $value) {
            $this->headers[strtolower($name)] = [$value];
        }
        $this->body = $body;
    }

    public function getStatusCode(): int
    {
        return $this->status;
    }

    public function withStatus(int $code, string $reasonPhrase = ''): ResponseInterface
    {
        $copy = clone $this;
        $copy->status = $code;
        return $copy;
    }

    public function getReasonPhrase(): string
    {
        return '';
    }
}

final class FakeFactory implements RequestFactoryInterface, StreamFactoryInterface
{
    public function createRequest(string $method, $uri): RequestInterface
    {
        return new FakeRequest($method, $uri instanceof UriInterface ? $uri : new FakeUri($uri));
    }

    public function createStream(string $content = ''): StreamInterface
    {
        return new FakeStream($content);
    }

    public function createStreamFromFile(string $filename, string $mode = 'r'): StreamInterface
    {
        throw new \RuntimeException('Not used by the bridge');
    }

    public function createStreamFromResource($resource): StreamInterface
    {
        throw new \RuntimeException('Not used by the bridge');
    }
}

final class FakeClientException extends \RuntimeException implements ClientExceptionInterface
{
}

/**
 * Returns the prepared response, or throws the prepared exception, for each request it receives. It never follows
 * a redirect and never sends a request of its own, so $requests holds exactly what the transport sent.
 */
final class FakeHttpClient implements ClientInterface
{
    /** @var list<RequestInterface> */
    public array $requests = [];

    public function __construct(private readonly ResponseInterface|\Throwable $answer)
    {
    }

    public function sendRequest(RequestInterface $request): ResponseInterface
    {
        $this->requests[] = $request;
        if ($this->answer instanceof \Throwable) {
            throw $this->answer;
        }
        return $this->answer;
    }
}
