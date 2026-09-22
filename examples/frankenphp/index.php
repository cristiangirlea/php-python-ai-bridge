<?php

declare(strict_types=1);

require dirname(__DIR__) . '/bootstrap.php';

use PhpAiBridge\BridgeException;
use PhpAiBridge\Client;

// A trusted, single-user demo. A real application must enforce user/job ownership.
$client = new Client(getenv('BRIDGE_URL') ?: 'http://worker:8090', getenv('BRIDGE_TOKEN') ?: '');
$handled = 0;
$handler = static function () use ($client, &$handled): void {
    ++$handled;
    header('Content-Type: application/json');
    header('Cache-Control: no-store');
    header('X-Demo-Worker-Requests: ' . $handled);
    try {
        $method = $_SERVER['REQUEST_METHOD'];
        $path = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);
        if ($method === 'GET' && $path === '/healthz') {
            echo json_encode(['status' => 'ok']);
            return;
        }
        if ($method === 'POST' && $path === '/rerank') {
            $raw = file_get_contents('php://input', false, null, 0, 262145);
            if ($raw === false || strlen($raw) > 262144) {
                http_response_code(413);
                echo json_encode(['error' => 'request_too_large']);
                return;
            }
            $body = json_decode($raw, true, 32, JSON_THROW_ON_ERROR);
            if (!is_array($body) || !is_string($body['query'] ?? null) || !is_array($body['documents'] ?? null)) {
                throw new InvalidArgumentException('Invalid rerank input');
            }
            $topK = $body['top_k'] ?? null;
            if ($topK !== null && !is_int($topK)) {
                throw new InvalidArgumentException('top_k must be an integer');
            }
            $job = $client->submitRerank($body['query'], $body['documents'], 30000, $topK);
            http_response_code(202);
        } elseif (preg_match('#\A/jobs/([a-f0-9]{32})(/cancel)?\z#', $path ?? '', $matches)) {
            if ($method === 'GET' && !isset($matches[2])) {
                $job = $client->get($matches[1]);
            } elseif ($method === 'POST' && isset($matches[2])) {
                $job = $client->cancel($matches[1]);
            } else {
                http_response_code(405);
                echo json_encode(['error' => 'method_not_allowed']);
                return;
            }
        } else {
            http_response_code(404);
            echo json_encode(['error' => 'not_found']);
            return;
        }
        echo json_encode($job->toArray(), JSON_THROW_ON_ERROR);
    } catch (JsonException | InvalidArgumentException) {
        http_response_code(400);
        echo json_encode(['error' => 'invalid_request']);
    } catch (BridgeException $error) {
        http_response_code(in_array($error->httpStatus, [400, 404, 413, 429], true) ? $error->httpStatus : 502);
        echo json_encode(['error' => $error->errorCode]);
    }
};

if (function_exists('frankenphp_handle_request')) {
    while (frankenphp_handle_request($handler)) {
        gc_collect_cycles();
    }
} else {
    $handler();
}
