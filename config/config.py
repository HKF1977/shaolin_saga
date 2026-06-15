from solders.pubkey import Pubkey

# System & pump.fun addresses
PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMP_GLOBAL = Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
PUMP_EVENT_AUTHORITY = Pubkey.from_string("Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1")
PUMP_FEE = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
SYSTEM_TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
SYSTEM_ASSOCIATED_TOKEN_ACCOUNT_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYSTEM_RENT = Pubkey.from_string("SysvarRent111111111111111111111111111111111")
SOL = Pubkey.from_string("So11111111111111111111111111111111111111112")
LAMPORTS_PER_SOL = 1_000_000_000

# Raydium addresses
RAYDIUM_AMM_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_CLMM_PROGRAM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
RAYDIUM_CP_SWAP_PROGRAM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
OPENBOOK_PROGRAM = "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX"

# Trading parameters
#BUY_AMOUNT = 0.0001  # Amount of SOL to spend when buying
#BUY_SLIPPAGE = 0.2  # 20% slippage tolerance for buying
#SELL_SLIPPAGE = 0.2  # 20% slippage tolerance for selling

# Your nodes
# You can also get a trader node https://docs.chainstack.com/docs/warp-transactions
RPC_ENDPOINT = "https://solana-mainnet.core.chainstack.com/82ab612622381d13630c6c49e807a097"
WSS_ENDPOINT = "wss://solana-mainnet.core.chainstack.com/82ab612622381d13630c6c49e807a097"

RPC_ENDPOINT_SECONDARY = "https://solana-mainnet.core.chainstack.com/dac4a0f01e1e265ad38ab7cf88a2f690"
WSS_ENDPOINT_SECONDARY = "wss://solana-mainnet.core.chainstack.com/dac4a0f01e1e265ad38ab7cf88a2f690"

RPC_BONDING_ENDPOINT = "https://solana-mainnet.core.chainstack.com/4014cf4d3ede9233e6b6d4a1ea49cb74"
WSS_BONDING_ENDPOINT = "wss://solana-mainnet.core.chainstack.com/4014cf4d3ede9233e6b6d4a1ea49cb74"

TOP_HOLDERS_RPC = "https://lb.drpc.live/solana/AlVCNjZMdkjnqEyazIkQl8RZ8omSY-gR8ZXPVjewFaCJ"

RPC_MOMENTUM_ENDPOINT = "https://solana-mainnet.core.chainstack.com/f26e353d42e11efa74782470e1fd0eb0" 
WSS_MOMENTUM_ENDPOINT = "wss://solana-mainnet.core.chainstack.com/f26e353d42e11efa74782470e1fd0eb0"

#Icon URL
SS_ICON_URL = "https://shaolinsaga.s3.eu-west-1.amazonaws.com/shaolinsaga_logo.png"

#Pump Livestreams
PUMP_LS_URL ="https://frontend-api-v3.pump.fun/coins/currently-live?offset=0&limit=1000&sort=currently_live&order=DESC&includeNsfw=true"

#Tweetscout api
TWEETSCOUT_KEY = "f097ae98-df3f-4fad-9203-ddef8ca83308"

#Moralis api
MORALIS_API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJub25jZSI6IjUxNGJjMzg2LWU3NDUtNDBhMi05NTQ0LTAzNTgyN2IxMDI5NyIsIm9yZ0lkIjoiNDc4NDUxIiwidXNlcklkIjoiNDkyMjMwIiwidHlwZUlkIjoiMzVlMGM1MzQtZThlZS00OWY1LTg0YzktZDg4MjE0MDk2ZTQ2IiwidHlwZSI6IlBST0pFQ1QiLCJpYXQiOjE3NjE3Mjg0MTUsImV4cCI6NDkxNzQ4ODQxNX0.cEUagbe0njNGAhm125bFpdUKTszKFVLG2i-GNVQ3H4Y"

#Private key
#PRIVATE_KEY = "SOLANA_PRIVATE_KEY"
