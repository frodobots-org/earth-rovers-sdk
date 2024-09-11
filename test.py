import requests
import concurrent.futures
import time

def make_request(url, params=None):
    start_time = time.time()
    response = requests.get(url, params=params)
    elapsed = time.time() - start_time
    return response.status_code, elapsed, params

def test_endpoint(url, num_requests, params=None):
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_requests) as executor:
        futures = [executor.submit(make_request, url, params) for _ in range(num_requests)]
        results = [future.result() for future in concurrent.futures.as_completed(futures)]

    return results

def main():
    base_url = "http://localhost:8000"  # Adjust this to your server's address
    num_requests = 20  # Number of concurrent requests to make for each endpoint/view type

    endpoints = [
        ("/", None),
        ("/data", None),
        ("/screenshot", {"view_types": "rear"}),
        ("/screenshot", {"view_types": "front"}),
        ("/screenshot", {"view_types": "map"}),
    ]

    for endpoint, params in endpoints:
        url = f"{base_url}{endpoint}"
        print(f"\nTesting endpoint: {url}")
        if params:
            print(f"With params: {params}")

        results = test_endpoint(url, num_requests, params)

        success_count = sum(1 for status, _, _ in results if status == 200)
        avg_time = sum(elapsed for _, elapsed, _ in results) / len(results)

        print(f"Successful requests: {success_count}/{num_requests}")
        print(f"Average response time: {avg_time:.4f} seconds")

if __name__ == "__main__":
    main()
