"""pip install 'langchain[mcp]>=1.4.0' httpx
Run with ELYREON_PAYLOAD_TOKEN saved from a trial enrollment. No model call needed.
"""
import asyncio,json,os
from fastmcp import Client
from langchain.mcp import MCPAdapter

async def main():
 async with MCPAdapter(Client("https://elyreon-payload-validator.colt2822.workers.dev/mcp",auth=os.environ["ELYREON_PAYLOAD_TOKEN"])) as adapter:
  tools=await adapter.list_tools()
  tool=next(t for t in tools if t.name=="validate_payload")
  result=await tool.ainvoke({"schema":{"type":"object","required":["id"],"properties":{"id":{"type":"integer"}}},"payload":{"id":42}})
  print(result)
if __name__=="__main__":asyncio.run(main())
