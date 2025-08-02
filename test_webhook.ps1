$body = @{
    app = "Skytide"
    payload = @{
        id = "test_123"
        type = "text"
        sender = @{
            phone = "573001234567"
            country_code = "57"
            dial_code = "3001234567"
        }
        payload = @{
            text = "Hola donde estan ubicados"
        }
    }
} | ConvertTo-Json -Depth 10

Write-Host "Sending payload:"
Write-Host $body

$response = Invoke-RestMethod -Uri "http://localhost:8080/webhooks/gupshup" -Method POST -Body $body -ContentType "application/json"
Write-Host "Response:"
Write-Host ($response | ConvertTo-Json)